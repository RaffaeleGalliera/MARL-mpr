import itertools
import logging
import math
import os
import pygame

import gymnasium
import networkx as nx
import torch
from gymnasium.utils import seeding
from pettingzoo import AECEnv
from pettingzoo.utils import wrappers, agent_selector
import numpy as np
from tianshou.data.batch import Batch

from .utils.wrappers.multi_discrete_to_discrete import MultiDiscreteToDiscreteWrapper

from .utils.constants import RADIUS_OF_INFLUENCE
from .utils.constants import NUMBER_OF_AGENTS
from .utils.core import Agent, MprWorld

from .graph import GraphEnv

import matplotlib.pyplot as plt


class MprEnv(GraphEnv):
    metadata = {
        'render_modes': ["human"],
        'name': "mpr_environment",
        'is_parallelizable': True
    }

    def __init__(
            self,
            number_of_agents=10,
            radius=10,
            max_cycles=5,
            device='cuda',
            graph=None,
            render_mode=None,
            local_ratio=None,
            seed=9,
            py_game=False
    ):
        super(AECEnv).__init__()
        self.py_game = py_game

        if self.py_game:
            pygame.init()
            self.game_font = pygame.freetype.Font(None, 24)
            self.viewer = None
            self.width = 1024
            self.height = 1024
            self.screen = pygame.Surface([self.width, self.height])
            self.max_size = 1
            plt.ion()
            plt.show()

        self.seed(seed)
        self.device = device

        self.render_mode = render_mode
        self.renderOn = False
        self.local_ratio = local_ratio
        self.radius = radius

        self.world = MprWorld(graph=graph,
                              number_of_agents=number_of_agents,
                              radius=radius,
                              np_random=self.np_random,
                              seed=seed,
                              is_scripted=False)

        # Needs to be a string for assertions check in tianshou
        self.agents = [agent.name for agent in self.world.agents]
        self.possible_agents = self.agents[:]
        self.agent_name_mapping = dict(
            zip(self.possible_agents,
                list(range(len(self.possible_agents))))
        )
        self._agent_selector = agent_selector(self.agents)
        self.max_cycles = max_cycles
        self.steps = 0
        self.current_actions = [None] * self.num_agents

        self.reset()

        # set spaces
        self.action_spaces = dict()
        self.observation_spaces = dict()

        actions_dim = np.zeros(NUMBER_OF_AGENTS)
        actions_dim.fill(2)
        for agent in self.world.agents:
            obs_dim = len(self.observe(agent.name)['observation'])

            self.action_spaces[agent.name] = gymnasium.spaces.MultiDiscrete(actions_dim)
            self.observation_spaces[agent.name] = gymnasium.spaces.Box(low=0, high=np.inf, shape=(obs_dim,))

    def enable_render(self, mode="human"):
        if not self.renderOn and mode == "human":
            self.screen = pygame.display.set_mode(self.screen.get_size())
            self.renderOn = True

    def draw(self, enable_aoi=False):
        # clear screen
        self.screen.fill((255, 255, 255))

        # update bounds to center around agent
        all_poses = [agent.pos for agent in self.world.agents]
        cam_range = np.max(np.abs(np.array(all_poses)))

        # update geometry and text positions
        text_line = 0
        for e, agent in enumerate(self.world.agents):
            # geometry
            x, y = agent.pos
            x_influence = ((x + RADIUS_OF_INFLUENCE) / cam_range) * self.width // 2 * 0.9
            x_influence += self.width // 2

            y *= (
                -1
            )  # this makes the display mimic the old pyglet setup (ie. flips image)
            x = (
                (x / cam_range) * self.width // 2 * 0.9
            )  # the .9 is just to keep entities from appearing "too" out-of-bounds
            y = (y / cam_range) * self.height // 2 * 0.9
            x += self.width // 2
            y += self.height // 2

            aoi_radius = math.dist([x, y], [x_influence, y])

            # Draw AoI
            aoi_color = (0, 255, 0, 128) if sum(agent.state.transmitted_to) else (255, 0, 0, 128)
            surface = pygame.Surface(self.screen.get_size(), pygame.SRCALPHA)
            pygame.draw.circle(surface, aoi_color, (x, y), aoi_radius)
            self.screen.blit(surface, (0, 0))

            entity_color = np.array([78, 237, 105]) if sum(agent.state.received_from) or agent.state.message_origin else agent.color
            pygame.draw.circle(
                self.screen, entity_color, (x, y), agent.size * 350
            )  # 350 is an arbitrary scale factor to get pygame to render similar sizes as pyglet
            pygame.draw.circle(
                self.screen, (0, 0, 0), (x, y), agent.size * 350, 1
            )  # borders
            assert (
                0 < x < self.width and 0 < y < self.height
            ), f"Coordinates {(x, y)} are out of bounds."

            # Draw agent name
            message = agent.name
            self.game_font.render_to(
                self.screen, (x, y), message, (0, 0, 0)
            )

            if isinstance(agent, Agent):
                if np.all(agent.state.relayed_by == 0):
                    word = "_"
                else:
                    indices = [i for i, x in enumerate(agent.state.relayed_by) if x == 1]
                    word = str(indices)

                message = agent.name + " chosen MPR " + word
                message_x_pos = self.width * 0.05
                message_y_pos = self.height * 0.95 - (self.height * 0.05 * text_line)
                self.game_font.render_to(
                   self.screen, (message_x_pos, message_y_pos), message, (0, 0, 0), bgcolor=entity_color
                )
                text_line += 1

    def observation(self, agent):
        agent_observation = agent.geometric_data

        # Every entry needs to be wrapped in a Batch object, otherwise
        # we will have shape errors in the data replay buffer
        edge_index = np.asarray(agent_observation.edge_index, dtype=np.int32)
        features = np.asarray(agent_observation.features, dtype=np.float32)
        labels = np.asarray(agent_observation.label, dtype=object)

        data = Batch.stack([Batch(observation=edge_index),
                            Batch(observation=labels),
                            Batch(observation=features),
                            Batch(observation=np.where(labels == agent.id))])

        return data

    def _set_action(self, action, agent, param):
        agent.action = np.zeros((self.num_agents,))
        agent.action = action[0]
        action = action[1:]
        assert len(action) == 0


def make_env(raw_env):
    def env(**kwargs):
        env = raw_env(**kwargs)
        # env = MultiDiscreteToDiscreteWrapper(env)
        # env = wrappers.AssertOutOfBoundsWrapper(env)
        env = wrappers.OrderEnforcingWrapper(env)
        return env

    return env


env = make_env(MprEnv)
