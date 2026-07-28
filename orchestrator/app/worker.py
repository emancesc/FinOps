"""
Worker RQ compatibile con Windows.
Usa TimerDeathPenalty (threading) invece di UnixSignalDeathPenalty (SIGALRM).
"""
from dotenv import load_dotenv
load_dotenv()

from rq import SimpleWorker
from rq.timeouts import TimerDeathPenalty


class WindowsWorker(SimpleWorker):
    death_penalty_class = TimerDeathPenalty
