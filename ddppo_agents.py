#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.


import argparse
from collections import OrderedDict
import random

import numpy as np
import numba
import torch
import PIL
from gym.spaces import Discrete, Dict, Box

import habitat
import os
from habitat_baselines.config.default import get_config
from habitat_baselines.rl.ppo import Policy, PointNavBaselinePolicy
from habitat_baselines.rl.ddppo.policy.resnet_policy import  PointNavResNetPolicy
from habitat_baselines.common.utils import batch_obs
from habitat import Config
from habitat.core.agent import Agent

@numba.njit
def _seed_numba(seed: int):
    random.seed(seed)
    np.random.seed(seed)

class DDPPOAgent(Agent):
    def __init__(self, config: Config):
        if "ObjectNav" in config.TASK_CONFIG.TASK.TYPE:
            OBJECT_CATEGORIES_NUM = 20
            spaces = {
                "objectgoal": Box(
                    low=0, 
                    high=OBJECT_CATEGORIES_NUM, 
                    shape=(1,), 
                    dtype=np.int64),
                "compass": Box(
                    low=-np.pi, 
                    high=np.pi, 
                    shape=(1,), 
                    dtype=np.float),
                "gps": Box(
                    low=np.finfo(np.float32).min,
                    high=np.finfo(np.float32).max,
                    shape=(2,),
                   dtype=np.float32,)
            }
        else:
            spaces = {
                "pointgoal": Box(
                    low=np.finfo(np.float32).min,
                    high=np.finfo(np.float32).max,
                    shape=(2,),
                    dtype=np.float32,
                )
            }

        if config.INPUT_TYPE in ["depth", "rgbd"]:
            spaces["depth"] = Box(
                low=0,
                high=1,
                shape=(config.TASK_CONFIG.SIMULATOR.DEPTH_SENSOR.HEIGHT,
                        config.TASK_CONFIG.SIMULATOR.DEPTH_SENSOR.WIDTH, 1),
                dtype=np.float32,
            )

        if config.INPUT_TYPE in ["rgb", "rgbd"]:
            spaces["rgb"] = Box(
                low=0,
                high=255,
                shape=(config.TASK_CONFIG.SIMULATOR.RGB_SENSOR.HEIGHT,
                        config.TASK_CONFIG.SIMULATOR.RGB_SENSOR.WIDTH, 3),
                dtype=np.uint8,
            )
        observation_spaces = Dict(spaces)

        action_space = Discrete(len(config.TASK_CONFIG.TASK.POSSIBLE_ACTIONS))

        self.device = torch.device("cuda:{}".format(config.TORCH_GPU_ID))
        self.hidden_size = config.RL.PPO.hidden_size

        random.seed(config.RANDOM_SEED)
        np.random.seed(config.RANDOM_SEED)
        _seed_numba(config.RANDOM_SEED)
        torch.random.manual_seed(config.RANDOM_SEED)
        torch.backends.cudnn.deterministic = True
        policy_arguments = OrderedDict(
            observation_space=observation_spaces,
            action_space=action_space,
            hidden_size=self.hidden_size,
            rnn_type=config.RL.DDPPO.rnn_type,
            num_recurrent_layers=config.RL.DDPPO.num_recurrent_layers,
            backbone=config.RL.DDPPO.backbone,
            normalize_visual_inputs="rgb" if config.INPUT_TYPE in ["rgb", "rgbd"] else False,
            final_beta=None,
            start_beta=None,
            beta_decay_steps=None,
            decay_start_step=None,
            use_info_bot=False,
            use_odometry=False,
        )
        if "ObjectNav" not in config.TASK_CONFIG.TASK.TYPE:
            policy_arguments["goal_sensor_uuid"] = config.TASK_CONFIG.TASK.GOAL_SENSOR_UUID

        self.actor_critic = PointNavResNetPolicy(**policy_arguments)
        self.actor_critic.to(self.device)
        self._encoder = self.actor_critic.net.visual_encoder

        if config.MODEL_PATH:
            ckpt = torch.load(config.MODEL_PATH, map_location=self.device)
            print(f"Checkpoint loaded: {config.MODEL_PATH}")
            #  Filter only actor_critic weights
            self.actor_critic.load_state_dict(
                {
                    k.replace("actor_critic.", ""): v
                    for k, v in ckpt["state_dict"].items()
                    if "actor_critic" in k
                }
            )

        else:
            habitat.logger.error(
                "Model checkpoint wasn't loaded, evaluating " "a random model."
            )

        self.test_recurrent_hidden_states = None
        self.not_done_masks = None
        self.prev_actions = None
        self.final_action = False

    def convertPolarToCartesian(self, coords):
        rho = coords[0]
        theta = -coords[1]
        return np.array([rho * np.cos(theta), rho * np.sin(theta)], dtype=np.float32)

    def convertMaxDepth(self, obs):
        # min_depth = 0.1
        # max_depth = 5
        # obs = obs * (10 - 0.1) + 0.1

        # if isinstance(obs, np.ndarray):
        #     obs = np.clip(obs, min_depth, max_depth)
        # else:
        #     obs = obs.clamp(min_depth, max_depth)

        # obs = (obs - min_depth) / (
        #     max_depth - min_depth
        # )

        return obs  

    def reset(self):
        self.test_recurrent_hidden_states = torch.zeros(
            self.actor_critic.net.num_recurrent_layers,
            1, self.hidden_size, device=self.device
        )
        self.not_done_masks = torch.zeros(1, 1, device=self.device)
        self.prev_actions = torch.zeros(
            1, 1, dtype=torch.long, device=self.device
        )
        self.prev_visual_features = None
        self.final_action = False

    def act(self, observations):
        observations["pointgoal"] = self.convertPolarToCartesian(observations["pointgoal"])
        observations["depth"] = self.convertMaxDepth(observations["depth"])
        batch = batch_obs([observations], device=self.device)
        batch["visual_features"] = self._encoder(batch)

        if self.prev_visual_features == None:
            batch["prev_visual_features"] = torch.zeros_like(batch["visual_features"])
        else:
            batch["prev_visual_features"] = self.prev_visual_features

        with torch.no_grad():
            step_batch = batch
            _, action, _, self.test_recurrent_hidden_states = self.actor_critic.act(
                batch,
                None,
                self.test_recurrent_hidden_states,
                self.prev_actions,
                self.not_done_masks,
                deterministic=False,
            )
            #  Make masks not done till reset (end of episode) will be called
            self.not_done_masks.fill_(1.0)
            self.prev_actions.copy_(action)
        
        self.prev_visual_features = step_batch["visual_features"]

        # if self.final_action:
        #     return 0
        
        # if action.item() == 0:
        #     self.final_action = True
        #     return 1

        return action.item()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input-type",
        default="blind",
        choices=["blind", "rgb", "depth", "rgbd"],
    )
    parser.add_argument("--evaluation", type=str, required=True, choices=["local", "remote"])
    config_paths = os.environ["CHALLENGE_CONFIG_FILE"]
    parser.add_argument("--model-path", default="", type=str)
    args = parser.parse_args()

    config = get_config('configs/ddppo_pointnav.yaml', 
                ['BASE_TASK_CONFIG_PATH', config_paths]).clone()
    config.defrost()
    config.TORCH_GPU_ID = 0
    config.INPUT_TYPE = args.input_type
    config.MODEL_PATH = args.model_path

    config.RANDOM_SEED = 7
    config.freeze()

    agent = DDPPOAgent(config)
    if args.evaluation == "local":
        challenge = habitat.Challenge(eval_remote=False)
        challenge._env.seed(config.RANDOM_SEED)
    else:
        challenge = habitat.Challenge(eval_remote=True)

    challenge.submit(agent)


if __name__ == "__main__":
    main()
