import pprint

import pytest
from main import get_args, train_agent, watch


# @pytest.mark.skip(reason="runtime too long and unstable result")
def test_mpr(args=get_args()):
    if args.watch:
        watch(args)
        return

    result, agent = train_agent(args)
    # assert result["best_reward"] >= 30.0

    if __name__ == '__main__':
        pprint.pprint(result)
        # Let's watch its performance!
        watch(args, agent)


if __name__ == '__main__':
    test_mpr(get_args())
