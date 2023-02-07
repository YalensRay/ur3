#!/usr/bin/env python
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from mushroom.algorithms.actor_critic import SAC
from mushroom.core import Core
from mushroom.environments.gym_env import Gym
from mushroom.utils.dataset import compute_J

from gym.envs.registration import register
from ur3e_openai.common import load_ros_params
import rospy

import signal
import sys

def signal_handler(sig, frame):
        print('You pressed Ctrl+C!')
        sys.exit(0)
signal.signal(signal.SIGINT, signal_handler)


class CriticNetwork(nn.Module):
    def __init__(self, input_shape, output_shape, n_features, **kwargs):
        super().__init__()

        n_input = input_shape[-1]
        n_output = output_shape[0]

        self._h1 = nn.Linear(n_input, n_features)
        self._h2 = nn.Linear(n_features, n_features)
        self._h3 = nn.Linear(n_features, n_output)

        nn.init.xavier_uniform_(self._h1.weight,
                                gain=nn.init.calculate_gain('relu'))
        nn.init.xavier_uniform_(self._h2.weight,
                                gain=nn.init.calculate_gain('relu'))
        nn.init.xavier_uniform_(self._h3.weight,
                                gain=nn.init.calculate_gain('linear'))

    def forward(self, state, action):
        state_action = torch.cat((state.float(), action.float()), dim=1)
        features1 = F.relu(self._h1(state_action))
        features2 = F.relu(self._h2(features1))
        q = self._h3(features2)

        return torch.squeeze(q)


class ActorNetwork(nn.Module):
    def __init__(self, input_shape, output_shape, n_features, **kwargs):
        super(ActorNetwork, self).__init__()

        n_input = input_shape[-1]
        n_output = output_shape[0]

        self._h1 = nn.Linear(n_input, n_features)
        self._h2 = nn.Linear(n_features, n_features)
        self._h3 = nn.Linear(n_features, n_output)

        nn.init.xavier_uniform_(self._h1.weight,
                                gain=nn.init.calculate_gain('relu'))
        nn.init.xavier_uniform_(self._h2.weight,
                                gain=nn.init.calculate_gain('relu'))
        nn.init.xavier_uniform_(self._h3.weight,
                                gain=nn.init.calculate_gain('linear'))

    def forward(self, state):
        features1 = F.relu(self._h1(torch.squeeze(state, 1).float()))
        features2 = F.relu(self._h2(features1))
        a = self._h3(features2)

        return a


def experiment(env, alg, n_epochs, n_steps, n_steps_test):
    np.random.seed()

    # MDP
    horizon = 100
    gamma = 0.99
    env_type = 'ros'
    mdp = Gym(env, env_type, horizon, gamma)

    # Settings
    initial_replay_size = 64
    max_replay_size = 50000
    batch_size = 64
    n_features = 64
    warmup_transitions = 100
    tau = 0.005
    lr_alpha = 3e-4

    use_cuda = torch.cuda.is_available()

    # Approximator
    actor_input_shape = mdp.info.observation_space.shape
    actor_mu_params = dict(network=ActorNetwork,
                           n_features=n_features,
                           input_shape=actor_input_shape,
                           output_shape=mdp.info.action_space.shape,
                           use_cuda=use_cuda)
    actor_sigma_params = dict(network=ActorNetwork,
                              n_features=n_features,
                              input_shape=actor_input_shape,
                              output_shape=mdp.info.action_space.shape,
                              use_cuda=use_cuda)

    actor_optimizer = {'class': optim.Adam,
                       'params': {'lr': 3e-4}}

    critic_input_shape = (actor_input_shape[0] + mdp.info.action_space.shape[0],)
    critic_params = dict(network=CriticNetwork,
                         optimizer={'class': optim.Adam,
                                    'params': {'lr': 3e-4}},
                         loss=F.mse_loss,
                         n_features=n_features,
                         input_shape=critic_input_shape,
                         output_shape=(1,),
                         use_cuda=use_cuda)

    # Agent
    agent = alg(mdp.info,
                batch_size, initial_replay_size, max_replay_size,
                warmup_transitions, tau, lr_alpha,
                actor_mu_params, actor_sigma_params,
                actor_optimizer, critic_params, critic_fit_params=None)

    # Algorithm
    core = Core(agent, mdp)

    core.learn(n_steps=initial_replay_size, n_steps_per_fit=initial_replay_size)

    mdp.reset()

    # RUN
    dataset = core.evaluate(n_steps=n_steps_test, render=False)
    J = compute_J(dataset, gamma)
    print('J: ', np.mean(J))

    # for n in range(n_epochs):
    #     print('Epoch: ', n)
    #     core.learn(n_steps=n_steps, n_steps_per_fit=1)
    #     dataset = core.evaluate(n_steps=n_steps_test, render=True)
    #     J = compute_J(dataset, gamma)
    #     print('J: ', np.mean(J))

    # print('Press a button to visualize pendulum')
    # input()
    # core.evaluate(n_episodes=5, render=True)


if __name__ == '__main__':
    rospy.init_node('ur3e_mushroom_sac',
                anonymous=True,
                log_level=rospy.DEBUG)
    load_ros_params(rospackage_name="ur3e_rl",
                    rel_path_from_package_to_file="config",
                    yaml_file_name="ur3e_ee_drl.yaml")
    env_name = rospy.get_param('/ur3e/env_name')
    register(
        id=env_name,
        entry_point='ur3e_openai.task_envs.peg_in_hole:UR3ePegInHoleEnv',
        max_episode_steps=100,
    )

    algs = [
        SAC
    ]

    for alg in algs:
        print('Algorithm: ', alg.__name__)
        experiment(env_name, alg=alg, n_epochs=20, n_steps=1000, n_steps_test=100)