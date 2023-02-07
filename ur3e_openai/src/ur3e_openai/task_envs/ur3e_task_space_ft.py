# The MIT License (MIT)
#
# Copyright (c) 2018-2022 Cristian C Beltran-Hernandez
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Author: Cristian C Beltran-Hernandez

import datetime
import rospy
import numpy as np

from ur_control.constants import FORCE_TORQUE_EXCEEDED
from ur_control import transformations, spalg
import ur3e_openai.cost_utils as cost

from gym import spaces

from ur3e_openai.robot_envs import ur3e_env
from ur3e_openai.control.parallel_controller import ParallelController
from ur3e_openai.control.admittance_controller import AdmittanceController
from ur3e_openai.robot_envs.utils import load_param_vars, save_log, randomize_initial_pose


class UR3eTaskSpaceFTEnv(ur3e_env.UR3eEnv):
    def __init__(self):

        self.cost_positive = False
        self.get_robot_params()

        ur3e_env.UR3eEnv.__init__(self)

        self._init_controller()
        self._previous_joints = None
        self.obs_logfile = None
        self.reward_per_step = []
        self.obs_per_step = []
        self.max_dist = None
        self.action_result = None

        self.last_actions = np.zeros(self.n_actions)
        obs = self._get_obs()

        self.reward_threshold = 500.0

        self.action_space = spaces.Box(-1., 1.,
                                       shape=(self.n_actions, ),
                                       dtype='float32')

        self.observation_space = spaces.Box(-np.inf,
                                            np.inf,
                                            shape=obs.shape,
                                            dtype='float32')

        self.trials = 1

        print("ACTION SPACES TYPE", (self.action_space))
        print("OBSERVATION SPACES TYPE", (self.observation_space))

    def get_robot_params(self):
        prefix = "ur3e_gym"
        load_param_vars(self, prefix)

        self.param_use_gazebo = False

        self.relative_to_ee = rospy.get_param(prefix + "/relative_to_ee", False)

        self.target_pose_uncertain = rospy.get_param(prefix + "/target_pose_uncertain", False)
        self.fixed_uncertainty_error = rospy.get_param(prefix + "/fixed_uncertainty_error", False)
        self.target_pose_uncertain_per_step = rospy.get_param(prefix + "/target_pose_uncertain_per_step", False)
        self.true_target_pose = rospy.get_param(prefix + "/target_pos", False)
        self.rand_seed = rospy.get_param(prefix + "/rand_seed", None)
        self.ft_hist = rospy.get_param(prefix + "/ft_hist", False)
        self.rand_init_interval = rospy.get_param(prefix + "/rand_init_interval", 5)
        self.rand_init_counter = 0
        self.rand_init_cpose = None
        self.insertion_direction = rospy.get_param(prefix + "/insertion_direction", 1)
        self.wrench_hist_size = rospy.get_param(prefix + "/wrench_hist_size", 12)
        self.randomize_desired_force = rospy.get_param(prefix + "/randomize_desired_force", False)
        self.test_mode = rospy.get_param(prefix + "/test_mode", False)

    def _init_controller(self):
        """
            Initialize controller
            position: direct task-space control
            parallel position-force: force control with parallel approach
            admittance: impedance on all task-space directions
        """

        if self.controller_type == "parallel_position_force":
            self.controller = ParallelController(self.ur3e_arm, self.agent_control_dt)
        elif self.controller_type == "admittance":
            self.controller = AdmittanceController(self.ur3e_arm, self.agent_control_dt)
        else:
            raise Exception("Unsupported controller" + self.controller_type)

    def _get_obs(self):
        """
        Here we define what sensor data of our robots observations
        To know which Variables we have acces to, we need to read the
        MyRobotEnv API DOCS
        :return: observations
        """
        joint_angles = self.ur3e_arm.joint_angles()
        ee_points, ee_velocities = self.get_points_and_vels(joint_angles)

        obs = None

        desired_force_wrt_goal = -1.0 * spalg.convert_wrench(self.controller.target_force_torque, self.target_pos)
        if self.ft_hist:
            force_torque = (self.ur3e_arm.get_ee_wrench_hist(self.wrench_hist_size) -
                            desired_force_wrt_goal) / self.controller.max_force_torque

            obs = np.concatenate([
                ee_points.ravel(),  # [6]
                ee_velocities.ravel(),  # [6]
                desired_force_wrt_goal[:3].ravel(),
                self.last_actions.ravel(),  # [14]
                force_torque.ravel(),  # [6]*24
            ])
        else:
            force_torque = (self.ur3e_arm.get_ee_wrench() - desired_force_wrt_goal) / self.controller.max_force_torque

            obs = np.concatenate([
                ee_points.ravel(),  # [6]
                ee_velocities.ravel(),  # [6]
                force_torque.ravel(),  # [6]
                self.last_actions.ravel(),  # [14]
            ])

        return obs.copy()

    def get_points_and_vels(self, joint_angles):
        """
        Helper function that gets the cartesian positions
        and velocities from ROS."""

        if self._previous_joints is None:
            self._previous_joints = self.ur3e_arm.joint_angles()

        # Current position
        ee_pos_now = self.ur3e_arm.end_effector(joint_angles=joint_angles)

        # Last position
        ee_pos_last = self.ur3e_arm.end_effector(joint_angles=self._previous_joints)
        self._previous_joints = joint_angles  # update

        # Use the past position to get the present velocity.
        linear_velocity = (ee_pos_now[:3] - ee_pos_last[:3]) / self.agent_control_dt
        angular_velocity = transformations.angular_velocity_from_quaternions(
            ee_pos_now[3:], ee_pos_last[3:], self.agent_control_dt)
        velocity = np.concatenate((linear_velocity, angular_velocity))

        # Shift the present poistion by the End Effector target.
        # Since we subtract the target point from the current position, the optimal
        # value for this will be 0.
        error = spalg.translation_rotation_error(self.target_pos, ee_pos_now)

        # scale error error, for more precise motion
        # (numerical error with small numbers?)
        error *= [1000, 1000, 1000, 1000., 1000., 1000.]

        # Extract only positions of interest
        if self.tgt_pose_indices is not None:
            error = np.array([error[i] for i in self.tgt_pose_indices])
            velocity = np.array([velocity[i] for i in self.tgt_pose_indices])

        return error, velocity

    def _set_init_pose(self):
        """Sets the Robot in its init pose
        """
        self._log()
        cpose = self.ur3e_arm.end_effector()
        deltax = np.array([0., 0., 0.02, 0., 0., 0.])
        cpose = transformations.pose_from_angular_velocity(cpose, deltax, dt=self.reset_time, rotated_frame=True)
        self.ur3e_arm.set_target_pose(pose=cpose,
                                      wait=True,
                                      t=self.reset_time)
        self._add_uncertainty_error()
        if self.random_initial_pose:
            self._randomize_initial_pose()
            self.ur3e_arm.set_target_pose(pose=self.rand_init_cpose,
                                          wait=True,
                                          t=self.reset_time)
        else:
            qc = self.init_q
            self.ur3e_arm.set_joint_positions(position=qc,
                                              wait=True,
                                              t=self.reset_time)
        self.ur3e_arm.set_wrench_offset(True)
        self._randomize_desired_force()
        self.controller.reset()
        self.max_distance = spalg.translation_rotation_error(self.ur3e_arm.end_effector(), self.target_pos) * 1000.
        self.max_dist = None

    def _randomize_initial_pose(self, override=False):
        if self.rand_init_cpose is None or self.rand_init_counter >= self.rand_init_interval or override:
            self.rand_init_cpose = randomize_initial_pose(
                self.ur3e_arm.end_effector(self.init_q), self.workspace, self.reset_time)
            self.rand_init_counter = 0
        self.rand_init_counter += 1

    def _randomize_desired_force(self, override=False):
        if self.randomize_desired_force:
            desired_force = np.zeros(6)
            desired_force[2] = np.abs(np.random.normal(scale=self.randomize_desired_force_scale))
            self.controller.target_force_torque = desired_force

    def _add_uncertainty_error(self):
        if self.target_pose_uncertain:
            if len(self.uncertainty_std) == 2:
                translation_error = np.random.normal(scale=self.uncertainty_std[0], size=3)
                translation_error[2] = 0.0
                rotation_error = np.random.normal(scale=self.uncertainty_std[1], size=3)
                rotation_error = np.deg2rad(rotation_error)
                error = np.concatenate([translation_error, rotation_error])
            elif len(self.uncertainty_std) == 6:
                if self.fixed_uncertainty_error:
                    error = self.uncertainty_std.copy()
                    error[3:] = np.deg2rad(error[3:])
                else:
                    translation_error = np.random.normal(scale=self.uncertainty_std[:3])
                    rotation_error = np.random.normal(scale=self.uncertainty_std[3:])
                    rotation_error = np.deg2rad(rotation_error)
                    error = np.concatenate([translation_error, rotation_error])
            else:
                print("Warning: invalid uncertanty error", self.uncertainty_std)
                return
            self.target_pos = transformations.transform_pose(self.true_target_pose, error, rotated_frame=True)

    def _log(self):
        # Test
        # log_data = np.array([self.controller.force_control_model.update_data,self.controller.force_control_model.error_data])
        # print("Hellooo",log_data.shape)
        # logfile = rospy.get_param("ur3e_gym/output_dir") + "/log_" + \
        #             datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '.npy'
        # np.save(logfile, log_data)
        if self.obs_logfile is None:
            try:
                self.obs_logfile = rospy.get_param("ur3e_gym/output_dir") + "/state_" + \
                    datetime.datetime.now().strftime('%Y%m%dT%H%M%S') + '.npy'
                print("obs_logfile", self.obs_logfile)
            except Exception:
                return
        # save_log(self.obs_logfile, self.obs_per_step, self.reward_per_step, self.cost_ws)
        self.reward_per_step = []
        self.obs_per_step = []

    def _compute_reward(self, observations, done):
        """
        Return the reward based on the observations given
        """
        state = []
        if self.ft_hist and not self.test_mode:
            ft_size = self.wrench_hist_size*6
            state = np.concatenate([
                observations[:-ft_size].ravel(),
                observations[-6:].ravel(),
                [self.action_result]
            ])
        else:
            state = np.concatenate([
                observations.ravel(),
                [self.action_result]
            ])
        self.obs_per_step.append([state])

        if self.reward_type == 'sparse':
            return cost.sparse(self, done)
        elif self.reward_type == 'distance':
            return -1 * cost.distance(self, observations, done)
        elif self.reward_type == 'force':
            return cost.distance_force_action_step_goal(self, observations, done)
        else:
            raise AssertionError("Unknown reward function", self.reward_type)

        return 0

    def _is_done(self, observations):
        if self.target_pose_uncertain_per_step:
            self._add_uncertainty_error()

        true_error = spalg.translation_rotation_error(self.true_target_pose, self.ur3e_arm.end_effector())
        true_error[:3] *= 1000.0
        true_error[3:] = np.rad2deg(true_error[3:])
        success = np.linalg.norm(true_error[:3], axis=-1) < self.distance_threshold
        self._log_message = "Final distance: " + str(np.round(true_error, 3)) + (' inserted!' if success else '')
        return success or self.action_result == FORCE_TORQUE_EXCEEDED

    def goal_distance(self, goal_a, goal_b):
        assert goal_a.shape == goal_b.shape
        return np.linalg.norm(goal_a - goal_b, axis=-1)

    def _init_env_variables(self):
        self.step_count = 0
        self.action_result = None
        self.last_actions = np.zeros(self.n_actions)

    def _set_action(self, action):
        self.action_result = self.controller.act(action, self.target_pos)