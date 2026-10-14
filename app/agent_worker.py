import sys
from .agent import execute

if __name__=="__main__":
    execute(sys.argv[1],sys.argv[2] if len(sys.argv)>2 else None)
