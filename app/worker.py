import sys
from .research import execute
from . import store

if __name__=="__main__":
    try:
        with store.execution_scope('run',sys.argv[1]): execute(sys.argv[1])
    except store.ExecutionStopped:
        pass
