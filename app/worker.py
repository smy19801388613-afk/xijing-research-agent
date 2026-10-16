import sys
from .research import execute
from . import budget_timeout, execution_budget, store

if __name__=="__main__":
    run_id=sys.argv[1]
    watchdog=budget_timeout.watch_run(run_id)
    try:
        with store.execution_scope('run',run_id): execute(run_id)
    except store.ExecutionStopped:
        pass
    except execution_budget.BudgetExhausted:
        pass  # The research boundary already persisted the exhausted budget.
    finally:
        watchdog.set()
        store.fail_execution('run',run_id)
