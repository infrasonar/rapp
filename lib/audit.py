from enum import Enum


class EventId(Enum):
    """EventId for audit logging.

    Ids for Rx in the range 6000..6999
    """

    RxStart = 6000
    RxSuccess = 6001
    RxFailed = 6002
