"""Send one short message to the phone through Claude News (used by bot.mjs for the pairing code
and for 'logged out' warnings). The message stays in the app's feed under "Claude News".

    python whatsapp/notify.py "title" "body"
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "worker"))

import claude_news_core as cn  # noqa: E402

if __name__ == "__main__":
    title, body = sys.argv[1], sys.argv[2]
    private = "--private" in sys.argv[3:]       # push to the phone only, nothing in the feed (for text copied from the group)
    summary, _ = cn.notify(title, body, project="Claude News", priority="high", feed=not private)
    print("notified:", {k: v for k, v in summary.items() if k != "devices"} if isinstance(summary, dict) else summary)
