from __future__ import annotations

import logging


class DispatcherLogFormatter(logging.Formatter):
    """日志格式器：缩短调度器 logger 前缀，输出短模块名。"""
    _PREFIX = "cairn.dispatcher."

    def format(self, record: logging.LogRecord) -> str:
        """剥前缀并注入 shortname 字段后交给父类格式化。"""
        name = record.name
        if name.startswith(self._PREFIX):
            shortname = name[len(self._PREFIX):]
        else:
            shortname = name
        setattr(record, "shortname", shortname)
        return super().format(record)


def configure_logging(level: str = "INFO", *, bare: bool = False) -> None:
    """初始化调度器日志：bare 模式只输出消息正文，默认带时间戳与模块名。"""
    handler = logging.StreamHandler()
    if bare:
        handler.setFormatter(logging.Formatter(fmt="%(message)s"))
    else:
        handler.setFormatter(
            DispatcherLogFormatter(
                fmt="[%(asctime)s] %(levelname)s %(shortname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=[handler], force=True)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
