import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token")

# slack_bolt.App はインスタンス化時にトークン検証を行うため、
# テスト時はモックに差し替える
mock_app = MagicMock()
mock_app.event = MagicMock(side_effect=lambda event_type: lambda f: f)
mock_app.middleware = MagicMock(side_effect=lambda f: f)

import slack_bolt  # noqa: E402

slack_bolt.App = MagicMock(return_value=mock_app)
