"""Modem exception hierarchy."""

from __future__ import annotations


class ModemError(Exception):
    """Base class for modem failures."""


class ModemNotConnected(ModemError):
    """The serial transport is not open."""


class ModemTimeout(ModemError):
    """The modem did not answer in time."""


class ModemCommandError(ModemError):
    """The modem answered ERROR / +CME ERROR / +CMS ERROR."""

    def __init__(self, command: str, final: str):
        super().__init__(f"{command} failed: {final}")
        self.command = command
        self.final = final


class SmsError(ModemError):
    """An SMS could not be encoded or sent."""
