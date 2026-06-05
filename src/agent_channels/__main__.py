"""Enable `python -m agent_channels`, used to launch the detached Slack worker."""
import sys

from agent_channels import main

if __name__ == "__main__":
    sys.exit(main())
