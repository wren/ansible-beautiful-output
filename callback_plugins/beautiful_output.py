# MIT License
# Copyright © 2022 Jonathan Wren <jonathan@nowandwren.com>
# Copyright (c) 2019 Thiago Alves

"""A clean and opinionated output callback plugin.

The goal of this plugin is to consolidated Ansible's output in the style of
LINUX/UNIX startup logs, and use unicode symbols to display task status.

This Callback plugin is intended to be used on playbooks that you have
to execute *in-person*, since it does always output to the screen.

In order to use this Callback plugin, you should add this Role as a dependency
in your project, and set the `stdout_callback` option on `ansible.cfg`

    stdout_callback = beautiful_output

"""
import json
import os
import re
import textwrap
from collections import OrderedDict
from collections.abc import Sequence
from enum import Enum
from enum import auto
from typing import TYPE_CHECKING
from typing import Callable
from typing import Optional

import yaml
from ansible import constants as C
from ansible import context
from ansible.module_utils._text import to_text
from ansible.module_utils.common._collections_compat import Mapping
from ansible.plugins.callback import CallbackBase
from ansible.template import Templar
from ansible.utils.color import stringc
from ansible.vars.clean import module_response_deepcopy
from ansible.vars.clean import strip_internal_keys

if TYPE_CHECKING:
    from ansible.executor.task_result import TaskResult
    from ansible.playbook import Playbook
    from ansible.playbook.play import Play
    from ansible.playbook.task import Task

DOCUMENTATION = """---
    callback: beautiful_output
    type: stdout
    author: Thiago Alves <thiago@rapinialves.com>
    short_description: a clean, condensed, and beautiful Ansible output
    version_added: 2.8
    description:
      - >-
        Consolidated Ansible output in the style of LINUX/UNIX startup
        logs, and use unicode symbols to organize tasks.
    extends_documentation_fragment:
      - default_callback
    requirements:
      - set as stdout in configuration
"""

TERMINAL_WIDTH = os.get_terminal_size().columns
DIVIDER = "─"

"""
A dictionary of symbols to be used when the Callback
needs to display a symbol on the screen.
"""
_symbol: dict[str, str] = {
    "success": to_text("🗹"),
    "warning": to_text("⚠"),
    "failure": to_text("🗷"),
    "dead": to_text("✝"),
    "empty": to_text("⬚"),
    "playbook": to_text("📖"),
    "retry": to_text("️↻"),
    "loop": to_text("∑"),
    "arrow_right": to_text("➞"),
    "skip": to_text("⬚"),
    "flag": to_text("🏷"),
}


"""A dictionary of terms used as section titles
when displaying the output of a command.
"""
_session_title: dict[str, str] = {
    "msg": "Message",
    "stdout": "Output",
    "stderr": "Error output",
    "module_stdout": "Module output",
    "module_stderr": "Module error output",
    "rc": "Return code",
    "changed": "Environment changed",
    "_ansible_no_log": "Omit logs",
    "use_stderr": "Use STDERR to output",
}


"""A dictionary representing the order in
which sections should be displayed to user.
"""
_session_order = OrderedDict(
    [
        ("_ansible_no_log", 3),
        ("use_stderr", 4),
        ("msg", 1),
        ("stdout", 1),
        ("module_stdout", 1),
        ("stderr", 1),
        ("module_stderr", 0),
        ("rc", 3),
        ("changed", 3),
    ]
)


"""A regular expression that can match any
ANSI escape sequence in a string.
"""
ansi_escape = re.compile(
    r"""
    \x1B    # ESC
    [@-_]   # 7-bit C1 Fe
    [0-?]*  # Parameter bytes
    [ -/]*  # Intermediate bytes
    [@-~]   # Final byte
""",
    re.VERBOSE,
)


"""Enum for possible statuses of a TaskResult."""


class TaskStatus(Enum):
    FAILED = auto()
    IGNORED = auto()
    OK = auto()
    SKIPPED = auto()
    CHANGED = auto()


def symbol(key: str, color: Optional[str] = None) -> str:
    """Helper function that returns an U)nicode character based on the given
    `key`. This function also colorize the returned string using the
     function, depending on the value passed to `color`.

    Returns a unicode character representing a symbol for the given `key`.
    """
    output = _symbol.get(key, to_text(":{0}:").format(key))
    if not color:
        return output
    return stringc(output, color)


def iscollection(obj: object) -> bool:
    """Helper method to check if a given object is not only a Sequence, but also
    **not** any kind of string.
    """
    return isinstance(obj, Sequence) and not isinstance(obj, str)


def stringtruncate(
    value: str,
    color: Optional[str] = "normal",
    width: Optional[int] = 0,
    justfn: Optional[Callable] = None,
    fillchar: Optional[str] = " ",
    truncate_placeholder: Optional[str] = "[...]",
):
    """Truncates a given string

    Args:
        value: A value to be truncated if it has more characters than is allowed.
        color: A string representing a color for Ansible.
        width: The allowed width (i.e. length of string) for `value`. If 0 is given, no
            truncation happens.
        justfn: A function to do the justification of the text. Defaults to str.rjust
            if `value` is integer and str.ljust otherwise.
        fillchar: The character used to fill the space up to `width` after (or before)
            the `value` content.
        truncate_placeholder: The text used to represent the truncation.

    Returns:
        A string truncated to `width` and aligned according to `justfn`.
    """
    if not value:
        return fillchar * width

    if not justfn:
        justfn = str.rjust if isinstance(value, int) else str.ljust

    if isinstance(value, int):
        value = to_text("{:n}").format(value)

    truncsize = len(truncate_placeholder)
    do_not_trucate = len(value) <= width or width == 0
    truncated_width = width - truncsize

    return stringc(
        to_text(justfn(str(value), width))
        if do_not_trucate
        else to_text(
            "{0}{1}".format(
                value[:truncated_width]
                if justfn == str.ljust
                else truncate_placeholder,
                truncate_placeholder
                if justfn == str.ljust
                else value[truncated_width:],
            )
        ),
        color,
    )


def dictsum(totals: dict[object, int], values: dict[object, int]):
    """Given two dictionaries of `int` values, this method will sum the
    value in `totals` with values in `values`.

    If a key in `values` does not exist in `totals`, that key will be
    added to it, and its initial value will be the same as in `values`.

    Note:
        The type of the keys in the dictionaries are irrelevant

    Args:
        totals: The total cached from previous calls of this functions.
        values: The dictionary of values used to sum up the totals.
    """
    for key, value in values.items():
        if key not in totals:
            totals[key] = value
        else:
            totals[key] += value


class CallbackModule(CallbackBase):
    """This class handles all Ansible callbacks that output text.

    See Also:
    - Ansible Callback documentation:
        https://docs.ansible.com/ansible/latest/plugins/callback.html
    - Ansible developping plugins documentation:
        https://docs.ansible.com/ansible/latest/dev_guide/developing_plugins.html#callback-plugins
    - Ansible Callback plugins from Ansible Core:
        https://github.com/ansible/ansible/tree/devel/lib/ansible/plugins/callback
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "beautiful_output"

    def __init__(self, display=None):
        CallbackBase.__init__(self, display)
        self.delegated_vars: dict
        self._item_processed: bool
        self._current_play: "Play" = None
        self._current_host: str
        self._task_name_buffer: str
        self.task_display_name: str
        self.should_display: bool = False

    def display(
        self,
        msg: str,
        color: Optional[str] = None,
        stderr: bool = False,
        newline: bool = True,
    ):
        """Helper method to display text on the screen.

        This method is a thin wrapper aroung the
        real ansible.utils.display.Display.display

        Any `msg` that is displayed with this method, will be displayed
        without any changes on the screen, and will have all the ANSI escape
        sequences stripped before displaying it on the logs.
        """
        self._display.display(
            msg=msg,
            color=color,
            stderr=stderr,
            screen_only=True,
            newline=newline,
        )
        self._display.display(
            msg=ansi_escape.sub("", msg),
            stderr=stderr,
            log_only=True,
        )

    def v2_playbook_on_start(self, playbook: "Playbook"):
        """Displays the Playbook report Header when Ansible starts."""
        playbook_name = stringc(
            os.path.basename(playbook._file_name), C.COLOR_HIGHLIGHT
        )
        if (
            "check" in context.CLIARGS
            and bool(context.CLIARGS["check"])
            and not self._is_run_verbose(verbosity=3)
            and not C.DISPLAY_ARGS_TO_STDOUT
        ):
            playbook_name = f"{playbook_name} (check mode)"

        self.display(to_text(f"{symbol('playbook')} Playbook: {playbook_name}"))

        # show CLI arguments
        if self._is_run_verbose(verbosity=3) or C.DISPLAY_ARGS_TO_STDOUT:
            self._display_cli_arguments()
        else:
            self._display_tag_strip(playbook)

    def v2_playbook_on_no_hosts_matched(self):
        """Display a warning when there is no hosts available."""
        self.display(
            "  %s No hosts found!" % symbol("warning", "bright yellow"),
            color=C.COLOR_DEBUG,
        )

    def v2_playbook_on_no_hosts_remaining(self):
        """Display an error when any hosts that were alive when it
        started running are not reachable anymore.
        """
        self.display(
            "  %s Ran out of hosts!" % symbol("warning", "bright red"),
            color=C.COLOR_ERROR,
        )

    def v2_playbook_on_play_start(self, play: "Play"):
        """Displays a banner with the play name and the hosts used in this
        play.

        This method might be called multiple times during the execution of a
        playbook, and it will not always have the play changed. Due to this
        fact, we short-circuit the method to not do anything if the play used
        to display the banner is the same as the one used on the last time the
        method was called.
        """
        if self._current_play:
            self._current_play = play
            return

        self._current_play = play
        name = play.get_name().strip()
        if name:
            self.display(to_text(f"[PLAY: {name}]").center(TERMINAL_WIDTH, DIVIDER))
        else:
            self.display("[PLAY]".center(TERMINAL_WIDTH, DIVIDER))

        if play.hosts:
            self.display("Hosts:")
            for host in play.hosts:
                self.display(
                    to_text("  - {0}").format(stringc(host, C.COLOR_HIGHLIGHT))
                )
            self.display(DIVIDER * TERMINAL_WIDTH)

    def v2_playbook_on_task_start(self, task: "Task", is_conditional: bool):
        """Displays a title for the given `task."""
        self._display_task_name(task)

    def v2_playbook_on_handler_task_start(self, task: "Task"):
        """Displays a title for the given `task, marking it as a handler task."""
        self._display_task_name(task, is_handler=True)

    def v2_runner_retry(self, result: "TaskResult"):
        """Displays the steps Ansible is retrying on a host."""
        msg = "  ️%s Retrying... (%d of %d)" % (
            symbol("retry"),
            result._result["attempts"],
            result._result["retries"],
        )
        if self._is_run_verbose(result, 2):
            # All result keys stating with _ansible_ are internal, so remove them from the result before we output anything.
            abridged_result = strip_internal_keys(
                module_response_deepcopy(result._result)
            )
            abridged_result.pop("exception", None)

            if not self._is_run_verbose(verbosity=3):
                abridged_result.pop("invocation", None)
                abridged_result.pop("diff", None)

            msg += "Result was: %s" % CallbackModule.dump_value(abridged_result)
        self.display(msg, color=C.COLOR_DEBUG)

    def v2_runner_on_start(self, host, task: "Task"):
        """Caches `host` object to be accessible during evaluation of a task."""
        self._current_host = host

    def v2_runner_on_ok(self, result: "TaskResult") -> None:
        """Displays the result of a task run.

        Note: This method will also be called every time an *item* in a loop
        task is processed.
        """
        if self._item_processed:
            return

        self._preprocess_result(result)
        msg, display_color = CallbackModule.changed_artifacts(result, "ok", C.COLOR_OK)
        task_result = self._process_result_output(
            result, msg, symbol("success"), display_color=display_color
        )
        if task_result:
            self.display(task_result, display_color)

    def v2_runner_on_skipped(self, result: "TaskResult"):
        """If configured to display skipped hosts, will display the skipped host."""
        if C.DISPLAY_SKIPPED_HOSTS:
            self._preprocess_result(result)
            task_result = self._process_result_output(
                result, "skipped", symbol("skip"), display_color=C.COLOR_SKIP
            )
            if task_result:
                self.display(task_result, C.COLOR_SKIP)
        else:
            self.outlines = []

    def v2_runner_on_failed(self, result: "TaskResult", ignore_errors: bool = False):
        """When a task fails, this method is called to display information
        about the error.
        """
        if self._item_processed:
            return

        self._preprocess_result(result)
        status = "ignored" if ignore_errors else "failed"
        color = C.COLOR_SKIP if ignore_errors else C.COLOR_ERROR
        task_result = self._process_result_output(
            result, status, symbol("failure"), display_color=color
        )
        if task_result:
            self.display(task_result, color)

    def v2_runner_on_unreachable(self, result: "TaskResult"):
        """When a host becames *unreachable* before the execution of its task,
        this method will display that information.
        """
        self._flush_display_buffer()
        task_result = self._process_result_output(
            result, "unreachable", symbol("dead"), display_color=C.COLOR_UNREACHABLE
        )
        if task_result:
            self.display(task_result, C.COLOR_UNREACHABLE)

    def v2_runner_item_on_ok(self, result: "TaskResult"):
        """Displays the result of a task run."""
        self._preprocess_result(result)
        status, display_color = CallbackModule.changed_artifacts(
            result, "ok", C.COLOR_OK
        )
        task_result = self._process_item_result_output(
            result,
            status,
            symbol("success"),
            display_color=display_color,
        )
        if task_result:
            self.display(task_result, display_color)

    def v2_runner_item_on_skipped(self, result: "TaskResult") -> None:
        """If configured to display skipped hosts, this method
        will display the skipped task."""
        if not C.DISPLAY_SKIPPED_HOSTS:
            self.outlines = []
            return

        self._preprocess_result(result)
        task_result = self._process_item_result_output(
            result, "skipped", symbol("skip"), display_color=C.COLOR_SKIP
        )
        if task_result:
            self.display(task_result, C.COLOR_SKIP)

    def v2_runner_item_on_failed(self, result: "TaskResult"):
        """When a task fails, this displays information about the failure."""
        self._flush_display_buffer()
        task_result = self._process_item_result_output(
            result, "failed", symbol("failure"), display_color=C.COLOR_ERROR
        )
        if task_result:
            self.display(task_result, C.COLOR_ERROR)

    def v2_playbook_on_stats(self, stats):
        """When the execution of a playbook finishes, this displays a summary.

        It also displays an aggregate total for all executions.
        """
        self.display(to_text("{0}\n\n").format(DIVIDER * TERMINAL_WIDTH))
        totals = {
            "ok": 0,
            "changed": 0,
            "unreachable": 0,
            "failures": 0,
            "rescued": 0,
            "ignored": 0,
        }

        self._display_summary_table_row(
            ("Hosts", C.COLOR_VERBOSE, 30),
            ("Success", C.COLOR_VERBOSE, 7),
            ("Changed", C.COLOR_VERBOSE, 7),
            ("Unreachable", C.COLOR_VERBOSE, 11),
            ("Failed", C.COLOR_VERBOSE, 6),
            ("Rescued", C.COLOR_VERBOSE, 7),
            ("Ignored", C.COLOR_VERBOSE, 7),
        )
        self._display_summary_table_separator("━")

        hosts = sorted(stats.processed.keys())
        host_summary = None
        for host_name in hosts:
            host_summary = stats.summarize(host_name)
            dictsum(totals, host_summary)
            self._display_summary_table_row(
                (host_name, C.COLOR_HIGHLIGHT, 30),
                (host_summary["ok"], C.COLOR_OK, 7),
                (host_summary["changed"], C.COLOR_CHANGED, 7),
                (host_summary["unreachable"] or 0, C.COLOR_UNREACHABLE, 11),
                (host_summary["failures"] or 0, C.COLOR_ERROR, 6),
                (host_summary["rescued"], C.COLOR_OK, 7),
                (host_summary["ignored"] or 0, C.COLOR_WARN, 7),
                #                (host_summary["ignored"], C.COLOR_WARN, 7),
            )

        self._display_summary_table_separator(DIVIDER)
        self._display_summary_table_row(
            ("Totals", C.COLOR_VERBOSE, 30),
            (totals["ok"], C.COLOR_OK, 7),
            (totals["changed"], C.COLOR_CHANGED, 7),
            (totals["unreachable"] or 0, C.COLOR_UNREACHABLE, 11),
            (totals["failures"] or 0, C.COLOR_ERROR, 6),
            (totals["rescued"], C.COLOR_OK, 7),
            (host_summary["ignored"] or 0, C.COLOR_WARN, 7),
        )

    def _handle_exception(self, result: dict["TaskResult"], use_stderr: bool = False):
        """When an exception happens during a playbook, this
        displays information about the crash.
        """
        if "exception" not in result:
            return

        result["use_stderr"] = use_stderr
        msg = "An exception occurred during task execution. "
        if not self._is_run_verbose(verbosity=3):
            # extract just the actual error message from the exception text

            error = result["exception"].strip().split("\n")[-1]
            msg += "To see the full traceback, use -vvv. The error was: %s" % error
        elif "module_stderr" in result:
            if result["exception"] != result["module_stderr"]:
                msg = "The full traceback is:\n" + result["exception"]
            del result["exception"]
        result["stderr"] = msg

    def _is_run_verbose(self, result: "TaskResult" = None, verbosity: int = 0) -> bool:
        """Verify if the current run is verbose (should display information)
        respecting the given level of `verbosity`.

        Returns True if the display verbosity >= `verbosity`, False otherwise.
        """
        result = {} if not result else result._result
        return (
            self._display.verbosity >= verbosity or "_ansible_verbose_always" in result
        ) and "_ansible_verbose_override" not in result

    def _display_cli_arguments(self, indent: int = 2):
        """Display all arguments passed to Ansible in the command line."""
        if context.CLIARGS.get("args"):
            self.display(
                to_text("{0}Positional arguments: {1}").format(
                    " " * indent, ", ".join(context.CLIARGS["args"])
                ),
                color=C.COLOR_VERBOSE,
            )

        for arg, val in {
            key: value
            for key, value in context.CLIARGS.items()
            if key != "args" and value
        }.items():
            if iscollection(val):
                self.display(
                    to_text("{0}{1}:").format(" " * indent, arg), color=C.COLOR_VERBOSE
                )
                for v in val:
                    self.display(
                        to_text("{0}- {1}").format(" " * (indent + 2), v),
                        color=C.COLOR_VERBOSE,
                    )
            else:
                self.display(
                    to_text("{0}{1}: {2}").format(" " * indent, arg, val),
                    color=C.COLOR_VERBOSE,
                )

    def _get_tags(self, playbook):
        """Returns a collection of tags that will be associated with all tasks
        runnin during this session.

        This means that it will collect all the tags available in the given
        `playbook`, and filter against the tags passed to Ansible in the
        command line.

        Args:
            playbook (:obj:`~ansible.playbook.Playbook`): The playbook where to
                look for tags.

        Returns:
            :obj:`list` of :obj:`str`: A sorted list of all tags used in this
            run.
        """
        tags = set()
        T = []
        for play in playbook.get_plays():
            for block in play.compile():
                blocks = block.filter_tagged_tasks({})
                if blocks.has_tasks():
                    for task in blocks.block:
                        tags.update(task.tags)
                        T.append(task.tags)

        """
        with open('/tmp/dat.tags_T','w') as f:
            f.write(simplejson.dumps(T))

        with open('/tmp/dat.tags_req','w') as f:
            f.write(simplejson.dumps(context.CLIARGS["tags"]))
        """

        if "tags" in context.CLIARGS:
            requested_tags = set(context.CLIARGS["tags"])
        else:
            requested_tags = {"all"}
        if len(requested_tags) > 1 or next(iter(requested_tags)) != "all":
            tags = tags.intersection(requested_tags)
        return sorted(tags)

    def _display_tag_strip(self, playbook: object, width: int = TERMINAL_WIDTH):
        """Displays the tags given in command that are also present in `playbook`

        If the line is longer than `width` characters, the line will wrap.
        """
        tags = self._get_tags(playbook)
        tag_strings = ""
        total_len = 0
        first_item = True
        for tag in sorted(tags):
            escape_code = "\x1b[30;47m"
            if not first_item:
                if total_len + len(tag) + 5 > width:
                    tag_strings += to_text("\n\n  {0} {1} {2} {3}").format(
                        escape_code, symbol("flag"), tag, "\x1b[0m"
                    )
                    total_len = len(tag) + 6
                    first_item = True
                else:
                    tag_strings += to_text(" {0} {1} {2} {3}").format(
                        escape_code, symbol("flag"), tag, "\x1b[0m"
                    )
                    total_len += len(tag) + 5
            else:
                first_item = False
                tag_strings += to_text("  {0} {1} {2} {3}").format(
                    escape_code, symbol("flag"), tag, "\x1b[0m"
                )
                total_len = len(tag) + 6
        self.display("\n")
        self.display(tag_strings)

    def _get_task_display_name(self, task: "Task"):
        """Caches the given `task` name if it is not an included task."""
        self.task_display_name = ""

        if task.name:
            self.task_display_name = str(task.name)
        elif task.action == "debug":
            self.task_display_name = str(task.action)

        self.should_display = self.task_display_name != ""

    def _preprocess_result(self, result: "TaskResult"):
        """Checks the result object for errors or warnings. Also makes sure
        that the task title buffer is flushed and displayed to the user.
        """
        self.delegated_vars = result._result.get("_ansible_delegated_vars", None)
        self._flush_display_buffer()
        self._handle_exception(result._result)
        self._handle_warnings(result._result)

    def _get_host_string(self, result: "TaskResult", prefix: str = "") -> str:
        """Retrieve the host from the given `result`.

        Returns a formatted version of the host that generated the `result`.
        """
        task_host = result._host.get_name()
        if task_host == "localhost":
            return ""
        task_host = f"{prefix}{task_host}"
        if self.delegated_vars:
            task_host += to_text(" {0} {1}{2}").format(
                symbol("arrow_right"), prefix, self.delegated_vars["ansible_host"]
            )
        return task_host

    def _process_result_output(
        self,
        result: "TaskResult",
        status: str,
        symbol_char: str = "",
        display_color: str = "",
    ) -> str:
        """Returns the result converted to string.

        Each key in the `result._result` is considered a session for the
        purpose of this method. All sessions have their content indented related
        to the session title.

        This method also converts all session titles that are present in the
        `const` dictionary, to their string representation. The rest of the
        titles are simply capitalized for aestetics purpose.

        Note: If a session verbosity does not cross the treshold for this
        playbook, it will not be shown.

        Args:
            result: TaskResult
            status: The status representing this output (e.g. "ok", "changed", "failed").
            symbol_char: An UTF-8 character to be used as at the start of output
            indent: How many characters the text generated from the `result` should be
                indended to.

        Returns a formated version of the given `result`.
        """
        if not self.should_display:
            # The title didn't display, so there's no status to update
            return ""

        task_result = ""
        task_host = self._get_host_string(result)

        if not self._item_processed:
            task_result = to_text("{0}{1}{2}").format(
                task_host,
                status.upper(),
                f"\r {symbol_char} \n" if symbol_char else "",
            )

        for key, verbosity in _session_order.items():
            if (
                key in result._result
                and result._result[key]
                and self._is_run_verbose(result, verbosity)
            ) or (status == "failed" and key == "msg"):
                task_result += self.reindent_session(
                    _session_title.get(key, key),
                    result._result[key],
                    color=display_color,
                )

        for title, text in result._result.items():
            if title not in _session_title and text and self._is_run_verbose(result, 2):
                task_result += self.reindent_session(
                    title.replace("_", " ").replace(".", " ").capitalize(),
                    text,
                    color=display_color,
                )

        return task_result

    def _process_item_result_output(
        self,
        result: "TaskResult",
        status: str,
        symbol_char: str = "",
        display_color: str = "",
    ) -> str:
        """Displays the given `result` of an item task.

        This method is a simplified version of the
        `_process_result_output` method where no sessions are printed.

        Args:
            result: TaskResult
            status: The status representing this output (e.g. "ok", "changed", "failed").
            symbol_char: An UTF-8 character to be used as at the start of output
            indent: How many characters the text generated from the `result` should be
                indended to.

        Returns a formated version of the given `result`.
        """
        if not self.should_display:
            return ""
        if not self._item_processed:
            # first item
            self.display("\r", newline=False)
            self._item_processed = True

        item_name = self._get_item_label(result._result)
        if isinstance(item_name, dict):
            if "name" in item_name:
                item_name = item_name.get("name")
            elif "path" in item_name:
                item_name = item_name.get("path")
            else:
                item_name = 'JSON: "{0}"'.format(
                    stringtruncate(
                        json.dumps(item_name, separators=(",", ":")), width=36
                    )
                )

        # prep output vars
        task_host = self._get_host_string(result, "@")
        host = f" ({task_host})" if task_host else ""

        # Error info
        error_info = ""
        if status == "failed":
            error_info = "\n" + self.reindent_session(
                _session_title["stderr"], result._result["msg"], color=display_color
            )

        return f" {symbol_char} {f'{self.my_role} ' or ''}{self.task_display_name}: {item_name}{host}... {stringc(status.upper(), display_color)}{error_info}"

    def _display_summary_table_separator(self, symbol_char):
        """Displays a line separating header or footer from content on the
        summary table.

        Args:
            symbol_char: An UTF-8 character to be used as a table separator
        """
        self.display(
            to_text(" {0} {1} {2} {3} {4} {5} {6}").format(
                symbol_char * 30,
                symbol_char * 7,
                symbol_char * 7,
                symbol_char * 11,
                symbol_char * 6,
                symbol_char * 7,
                symbol_char * 7,
            )
        )

    def _display_summary_table_row(
        self,
        host: tuple[str, str, int],
        success: tuple[str, str, int],
        changed: tuple[str, str, int],
        dark: tuple[str, str, int],
        failed: tuple[str, str, int],
        rescued: tuple[str, str, int],
        ignored: tuple[str, str, int],
    ):
        """Displays a single line in the summary table, respecting the color and
        size given in the arguments.

        Each argument in this method is a tuple of three values:

        - The text;
        - The color;
        - The width;

        Args:
            host: Which host this row is representing.
            success: How many tasks were run successfully.
            changed: How many values were changed due to playbook.
            dark: How many hosts were not reachable during execution.
            failed: How many tasks failed during their execution.
            rescued: How many tasks were recovered from a failure and
                completed successfully.
            ignored: How many tasks were ignored.
        """
        self.display(
            to_text(" {0} {1} {2} {3} {4} {5} {6}").format(
                stringtruncate(host[0], host[1], host[2]),
                stringtruncate(success[0], success[1], success[2]),
                stringtruncate(changed[0], changed[1], changed[2]),
                stringtruncate(dark[0], dark[1], dark[2]),
                stringtruncate(failed[0], failed[1], failed[2]),
                stringtruncate(rescued[0], rescued[1], rescued[2]),
                stringtruncate(ignored[0], ignored[1], ignored[2]),
            )
        )

    def _display_task_decision_score(self, task: "Task") -> float:
        """Calculate the probability for the given `task` to be displayed
        based on configurations and the task `when` clause.

        Returns a number between 0 and 1 representing the probability to show
            the given `task`. Currently this method only return 3 possible
            values:

            Examples of return values:
            - 0.1: We are sure that the task should not be displayed
            - 0.5: We don't know if this task should be displayed or not. By
                default, we associate any `task` with this score, and change
                it as needed
            - 1.0: We are sure the `task` should be displayed.
        """
        score = 0.5
        var_manager = task.get_variable_manager()
        task_args = task.args
        if task.when and var_manager:
            all_hosts = CallbackModule.get_chained_value(
                var_manager.get_vars(), "hostvars"
            )
            play_task_vars = var_manager.get_vars(
                play=self._current_play, host=self._current_host, task=task
            )
            templar = Templar(task._loader, variables=play_task_vars)
            exception = False
            for hostname in all_hosts.keys():
                host_vars = CallbackModule.get_chained_value(all_hosts, hostname)
                host_vars.update(play_task_vars)
                try:
                    if not task.evaluate_conditional(templar, host_vars):
                        score = 0.0
                        break
                except Exception:
                    exception = True
            else:
                if not exception:
                    score = 1.0
        elif task.action == "debug" and task_args and "verbosity" in task_args:
            score = (
                1.0
                if self._is_run_verbose(verbosity=int(task_args["verbosity"]))
                else 0.0
            )

        if not task.name and task.action != "debug":
            score = 0.0

        return score

    def _display_task_name(self, task: "Task", is_handler=False):
        """(Maybe) Displays the given `task` title (if given options permit)."""
        self._item_processed = False
        self._get_task_display_name(task)

        if not self.task_display_name:
            return

        temp_name = self.task_display_name

        if task._role:
            my_role = task._role.get_name() or ""
            formatted_role = stringc(f"{my_role} |", "dark gray")
            self.my_role = formatted_role
            temp_name = f"{formatted_role} {temp_name}"

        if is_handler:
            temp_name = f"{temp_name} (via handler)"

        # Add symbol and dots
        temp_name = f" {symbol('empty')} {temp_name}... "

        self._task_name_buffer = temp_name

        display_score = self._display_task_decision_score(task)
        if display_score >= 1.0 or C.DISPLAY_SKIPPED_HOSTS:
            self._flush_display_buffer()
        elif display_score < 0.1:
            self._task_name_buffer = None

    def _flush_display_buffer(self):
        """Display a task title if there is one to display."""
        if not self._task_name_buffer:
            return

        self.display(self._task_name_buffer, newline=False)
        self._task_name_buffer = None

    @staticmethod
    def try_parse_string(text: str):
        """This method will try to parse the given `text` using JSON and
        YAML parsers in order to return a dictionary representing the parsed
        structure.

        Returns the parsed object from `text`. If the given `text` was
            not a JSON or YAML content, `None` will be returned.
        """
        textobj = None

        try:
            textobj = json.loads(text)
        except Exception as e:
            try:
                textobj = yaml.load(text, Loader=yaml.SafeLoader)
            except Exception:
                pass

        return textobj

    @staticmethod
    def dump_value(value: str) -> str:
        """Given a string, this method will parse the given string and return
        the parsed object converted to a YAML representation.
        """
        text = None
        obj = CallbackModule.try_parse_string(value)
        if obj:
            text = yaml.dump(obj, Dumper=yaml.SafeDumper, default_flow_style=False)
        return text

    def reindent_session(
        self, title: str, text: str, width: int = TERMINAL_WIDTH, color: str = ""
    ):
        """This method returns a text formatted with the given `indent` and
        wrapped at the given `width`.
        """
        textwidth = width - len(title)
        textstr = str(text).strip()
        dumped = False
        if textstr.startswith("---") or textstr.startswith("{"):
            dumped = CallbackModule.dump_value(textstr)
            textstr = dumped if dumped else textstr
        lines = textstr.splitlines()

        formatted_title = stringc(f"{title}:", color) if color else title
        output = f"   {self.my_role} {formatted_title}\n"

        if (len(lines) == 1) and (len(textstr) <= textwidth) and (not dumped):
            formatted_line = stringc(textstr, color) if color else textstr
            output += f"   {self.my_role} {formatted_line}"
            return output

        for line in lines:
            formatted_line = textwrap.fill(text=line, width=width - len(self.my_role))
            formatted_line = stringc(formatted_line, color) if color else formatted_line
            formatted_lines = f"   {self.my_role} {formatted_line}".split("\n")
            output += f"\n   {self.my_role} ".join(formatted_lines)

        return output

    @staticmethod
    def changed_artifacts(
        result: "TaskResult", status: str, display_color: str
    ) -> tuple[str, str]:
        """Detect if the given `result` did change anything during its
        execution and return the proper status and display color for it.

        Returns (status, display_color)
        Example: ("changed", "yellow")
        """
        result_was_changed = "changed" in result._result and result._result["changed"]
        if result_was_changed:
            return "changed", C.COLOR_CHANGED
        return status, display_color

    @staticmethod
    def get_chained_value(mapping: dict, *args):
        """Returns a value from a dictionary.

        It can return chained values based on a list of keys given by the
        `args` argument.

        Example:
            >>> nested_dict = {
            ...     "a_key": "a_value",
            ...     "dict_key": {
            ...         "other_key": "other_value",
            ...         "other_dict_key": {
            ...             "target_value": "Found It!"
            ...         }
            ...     }
            ... }
            >>> CallbackModule.get_chained_value(nested_dict, "dict_key", "other_dict_key", "target_value")
            'Found It!'
        """
        if args:
            key = args[0]
            others = args[1:]

            if key in mapping:
                value = mapping[key]
                if others:
                    return CallbackModule.get_chained_value(value, *others)
                if isinstance(value, Mapping):
                    dict_value = {}
                    dict_value.update(value)
                    return dict_value
                return value
        return None
