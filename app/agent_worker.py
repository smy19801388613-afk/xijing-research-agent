import sys
from .agent import execute
from . import budget_timeout, store

if __name__=="__main__":
    cid=sys.argv[1]
    turn_id=sys.argv[2] if len(sys.argv)>2 else store.get_conversation(cid).get('turn_id')
    watchdog=budget_timeout.watch_turn(cid,turn_id)
    try:
        execute(cid,turn_id)
    finally:
        watchdog.set()
