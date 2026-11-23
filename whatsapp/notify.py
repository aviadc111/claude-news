"""Send one short message to the phone through Claude News (used by bot.mjs for the pairing code
and for 'logged out' warnings). The message is a push only and is not kept in the app's feed.

    python whatsapp/notify.py "title" "body"
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "worker"))

import claude_news_core as cn  # noqa: E402

if __name__ == "__main__":
    title, body = sys.argv[1], sys.argv[2]
    summary, _ = cn.notify(title, body, project="Claude News", priority="high", feed=False, ephemeral=True)
    print("notified:", {k: v for k, v in summary.items() if k != "devices"} if isinstance(summary, dict) else summary)
