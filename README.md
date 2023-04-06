# [WIP] A Multi Agent Reinforcement Learning Environment for Message Dissemination
 
- Build the Dockerfile `docker build -t <name> .` 
- Run for example with:`docker run --gpus all --ipc=host --ulimit memlock=-1 --ulimit stack=67108864 -v ${PWD}:/home/devuser/dev:Z  -e DISPLAY=$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix -it --rm  <name>`
- Start Training: `python main.py`
- Watch the agent: `python main.py --watch` 