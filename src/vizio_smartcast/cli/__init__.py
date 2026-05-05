"""
Typer-based CLI entry point: ``vizio-smartcast ...``.

Global flags (set on the parent ``app``):
- ``--device NAME`` — alias from the config file
- ``--host HOST`` — ad-hoc IP[:PORT] (overrides --device)
- ``--auth TOKEN`` — ad-hoc auth token
- ``--device-type {tv,soundbar,crave_go,crave360,crave_pro}``
- ``--config PATH`` — override config path (also via $VIZIO_SMARTCAST_CONFIG)
- ``--format {table,tsv,json,plain}`` — output format (auto by default)
- ``-v``/``--verbose`` — debug logging

Resolution order: --host > --device > config.default_device.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from pathlib import Path
import shlex
from typing import Annotated, Any

from rich.console import Console
import typer

from .. import (
    AppConfig,
    DeviceType,
    PairChallenge,
    Vizio,
    VizioError,
    VizioInvalidInputError,
)
from ..discovery import discover
from ._config import Config, DeviceRecord
from ._format import (
    OutputFormat,
    render_message,
    render_rows,
    render_value,
)
from ._resolve import (
    CLIResolutionError,
    ResolvedDevice,
    resolve_device,
)

# ---------------------------------------------------------------------------
# State carried via typer's Context for nested commands.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CLIState:
    config: Config
    host_override: str | None
    device_alias: str | None
    device_type_override: DeviceType | None
    auth_override: str | None
    output_format: OutputFormat | None

    def resolve(self) -> ResolvedDevice:
        return resolve_device(
            host=self.host_override,
            device_alias=self.device_alias,
            device_type=self.device_type_override,
            auth_token=self.auth_override,
            config=self.config,
        )


# ---------------------------------------------------------------------------
# Top-level app + global options
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="vizio-smartcast",
    help="Control Vizio SmartCast devices from the command line.",
    add_completion=True,
    no_args_is_help=True,
)


@app.callback()
def _main(
    ctx: typer.Context,
    device: Annotated[
        str | None,
        typer.Option("--device", help="Saved device alias from the config."),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option(
            "--host", help="Ad-hoc target host (IP or IP:PORT). Overrides --device."
        ),
    ] = None,
    auth: Annotated[
        str | None,
        typer.Option("--auth", help="Ad-hoc auth token; overrides any saved token."),
    ] = None,
    device_type: Annotated[
        DeviceType | None,
        typer.Option("--device-type", help="Override the device family."),
    ] = None,
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Override config file location.",
            envvar="VIZIO_SMARTCAST_CONFIG",
        ),
    ] = None,
    output_format: Annotated[
        OutputFormat | None,
        typer.Option("--format", help="Output format (auto by default)."),
    ] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Enable debug logging.")
    ] = False,
) -> None:
    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    ctx.obj = CLIState(
        config=Config.load(config_path),
        host_override=host,
        device_alias=device,
        device_type_override=device_type,
        auth_override=auth,
        output_format=output_format,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_err = Console(stderr=True, style="red")


def _state(ctx: typer.Context) -> CLIState:
    return ctx.obj  # type: ignore[no-any-return]


def _print(value: str) -> None:
    if value:
        print(value)


# Reusable ``--format`` option for every output-producing leaf command.
# Lets users put ``--format json`` after the subcommand name (matching
# git/kubectl/docker convention), instead of forcing it to the global
# slot before the subcommand. When supplied at the leaf, overrides the
# global ``--format``; otherwise inherits.
FormatOption = Annotated[
    OutputFormat | None,
    typer.Option(
        "--format",
        help="Output format. Overrides global --format. Defaults to "
        "table on TTY, TSV when piped.",
    ),
]


def _fmt(ctx: typer.Context, leaf: OutputFormat | None) -> OutputFormat | None:
    """Resolve effective format: leaf-level wins over global."""
    return leaf if leaf is not None else _state(ctx).output_format


def _exec[T](ctx: typer.Context, fn: Callable[[Vizio], Awaitable[T]]) -> T:
    """
    Run ``fn(vizio)`` against the resolved device.

    Surfaces :class:`VizioError` as exit code 1 and resolution errors as
    exit code 2.
    """
    state = _state(ctx)
    try:
        target = state.resolve()
    except CLIResolutionError as e:
        _err.print(str(e))
        raise typer.Exit(code=2) from e

    async def _go() -> T:
        async with Vizio(
            host=target.host,
            device_type=target.device_type,
            auth_token=target.auth_token,
        ) as v:
            return await fn(v)

    try:
        return asyncio.run(_go())
    except VizioInvalidInputError as e:
        _err.print(str(e))
        raise typer.Exit(code=1) from e
    except VizioError as e:
        _err.print(f"vizio-smartcast: {e}")
        raise typer.Exit(code=1) from e


# ---------------------------------------------------------------------------
# `vizio-smartcast device ...` — alias management
# ---------------------------------------------------------------------------

device_app = typer.Typer(name="device", help="Manage saved device aliases.")
app.add_typer(device_app)


@device_app.command("add")
def device_add(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Alias name.")],
    host: Annotated[str, typer.Option("--host", help="IP or IP:PORT.")],
    device_type: Annotated[
        DeviceType,
        typer.Option("--device-type", help="Device family."),
    ] = DeviceType.TV,
    auth: Annotated[
        str | None,
        typer.Option("--auth", help="Auth token (TVs require this)."),
    ] = None,
    output_format: FormatOption = None,
) -> None:
    """Save a device under a memorable alias."""
    state = _state(ctx)
    state.config.add_device(
        DeviceRecord(name=name, host=host, device_type=device_type, auth_token=auth)
    )
    state.config.save()
    _print(render_message(f"Saved {name!r}", fmt=_fmt(ctx, output_format)))


@device_app.command("remove")
def device_remove(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Alias to remove.")],
    output_format: FormatOption = None,
) -> None:
    state = _state(ctx)
    state.config.remove_device(name)
    state.config.save()
    _print(render_message(f"Removed {name!r}", fmt=_fmt(ctx, output_format)))


@device_app.command("list")
def device_list(ctx: typer.Context, output_format: FormatOption = None) -> None:
    state = _state(ctx)
    rows = [
        {
            "name": r.name,
            "host": r.host,
            "device_type": r.device_type.value,
            "default": r.name == state.config.default_device,
            "auth": "yes" if r.auth_token else "",
        }
        for r in state.config.list_devices()
    ]
    _print(render_rows(rows, fmt=_fmt(ctx, output_format)))


@device_app.command("set-default")
def device_set_default(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Alias to set as default.")],
    output_format: FormatOption = None,
) -> None:
    state = _state(ctx)
    if name not in state.config:
        _err.print(f"No alias {name!r}")
        raise typer.Exit(code=2)
    state.config.default_device = name
    state.config.save()
    _print(render_message(f"Default = {name!r}", fmt=_fmt(ctx, output_format)))


@device_app.command("show")
def device_show(
    ctx: typer.Context,
    name: Annotated[str | None, typer.Argument()] = None,
    output_format: FormatOption = None,
) -> None:
    state = _state(ctx)
    target = name or state.config.default_device
    if target is None:
        _err.print("No alias specified and no default set.")
        raise typer.Exit(code=2)
    try:
        record = state.config.get_device(target)
    except KeyError as e:
        _err.print(f"No alias {target!r}")
        raise typer.Exit(code=2) from e
    _print(
        render_rows(
            [
                {
                    "name": record.name,
                    "host": record.host,
                    "device_type": record.device_type.value,
                    "auth": "yes" if record.auth_token else "",
                }
            ],
            fmt=_fmt(ctx, output_format),
        )
    )


# ---------------------------------------------------------------------------
# `vizio-smartcast discover`
# ---------------------------------------------------------------------------


@app.command("discover")
def discover_cmd(
    ctx: typer.Context,
    timeout: Annotated[float, typer.Option("--timeout")] = 5.0,
    no_ssdp: Annotated[
        bool, typer.Option("--no-ssdp", help="Skip SSDP fallback.")
    ] = False,
    output_format: FormatOption = None,
) -> None:
    """Discover Vizio devices on the local network."""

    async def _go() -> list[dict[str, Any]]:
        devices = await discover(timeout=timeout, include_ssdp=not no_ssdp)
        return [
            {"name": d.name, "host": d.host, "model": d.model, "id": d.id}
            for d in devices
        ]

    rows = asyncio.run(_go())
    if not rows:
        _err.print("No Vizio devices found.")
        raise typer.Exit(code=1)
    _print(render_rows(rows, fmt=_fmt(ctx, output_format)))


# ---------------------------------------------------------------------------
# `vizio-smartcast pair`
# ---------------------------------------------------------------------------


pair_app = typer.Typer(name="pair", help="Device pairing operations.")
app.add_typer(pair_app)


@pair_app.command("begin")
def pair_begin(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Device IP or IP:PORT.")],
    device_type: Annotated[
        DeviceType, typer.Option("--device-type", help="Device family.")
    ] = DeviceType.TV,
    device_id: Annotated[
        str,
        typer.Option("--device-id", help="Client device ID sent to the TV."),
    ] = "vizio-cli",
    device_name: Annotated[
        str,
        typer.Option("--device-name", help="Client device name sent to the TV."),
    ] = "vizio-smartcast CLI",
    output_format: FormatOption = None,
) -> None:
    """
    Start a pairing session and output the challenge data.

    After running this command, complete pairing with ``pair complete``.
    """
    fmt = _fmt(ctx, output_format)

    async def _go() -> PairChallenge:
        async with Vizio(host=host, device_type=device_type) as v:
            return await v.begin_pair(device_id=device_id, device_name=device_name)

    try:
        challenge = asyncio.run(_go())
    except VizioError as e:
        _err.print(f"Pairing begin failed: {e}")
        raise typer.Exit(code=1) from e

    hint = (
        f"vizio-smartcast pair complete {shlex.quote(host)}"
        f" --device-type {device_type.value}"
        f" --device-id {shlex.quote(device_id)}"
        f" --challenge-type {challenge.challenge_type}"
        f" --pairing-token {challenge.token}"
        f" --pin <PIN>"
    )

    if fmt is OutputFormat.JSON:
        _print(
            render_value(
                {
                    "challenge_type": challenge.challenge_type,
                    "pairing_token": challenge.token,
                    "next_step": hint,
                },
                fmt=fmt,
            )
        )
    else:
        _err.print("PIN should appear on the device.")
        _print(
            render_rows(
                [
                    {
                        "challenge_type": challenge.challenge_type,
                        "pairing_token": challenge.token,
                    }
                ],
                fmt=fmt,
            )
        )
        _err.print(f"\nNext step: {hint}")


@pair_app.command("complete")
def pair_complete(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Device IP or IP:PORT.")],
    challenge_type: Annotated[
        int,
        typer.Option("--challenge-type", help="From pair begin output."),
    ],
    pairing_token: Annotated[
        int,
        typer.Option("--pairing-token", help="From pair begin output."),
    ],
    pin: Annotated[
        str,
        typer.Option("--pin", help="PIN displayed on the device."),
    ],
    device_type: Annotated[
        DeviceType, typer.Option("--device-type", help="Device family.")
    ] = DeviceType.TV,
    device_id: Annotated[
        str,
        typer.Option(
            "--device-id",
            help="Must match the device_id used in pair begin.",
        ),
    ] = "vizio-cli",
    save_as: Annotated[
        str | None,
        typer.Option("--save-as", help="Save the resulting alias for later."),
    ] = None,
    output_format: FormatOption = None,
) -> None:
    """Complete pairing with the PIN and challenge data from ``pair begin``."""
    state = _state(ctx)
    fmt = _fmt(ctx, output_format)

    async def _go() -> str:
        async with Vizio(host=host, device_type=device_type) as v:
            return await v.finish_pair(
                device_id=device_id,
                challenge_type=challenge_type,
                token=pairing_token,
                pin=pin,
            )

    try:
        token = asyncio.run(_go())
    except VizioError as e:
        _err.print(f"Pairing complete failed: {e}")
        raise typer.Exit(code=1) from e

    if save_as:
        state.config.add_device(
            DeviceRecord(
                name=save_as,
                host=host,
                device_type=device_type,
                auth_token=token,
            )
        )
        state.config.save()
        _print(render_message(f"Paired and saved as {save_as!r}", fmt=fmt))
    elif fmt is OutputFormat.JSON:
        _print(render_value({"auth_token": token}, fmt=fmt))
    else:
        _print(render_rows([{"auth_token": token}], fmt=fmt))


@pair_app.command("cancel")
def pair_cancel(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Device IP or IP:PORT.")],
    device_type: Annotated[
        DeviceType, typer.Option("--device-type", help="Device family.")
    ] = DeviceType.TV,
    device_id: Annotated[
        str,
        typer.Option(
            "--device-id",
            help="Must match the device_id used in pair begin.",
        ),
    ] = "vizio-cli",
    device_name: Annotated[
        str,
        typer.Option(
            "--device-name",
            help="Must match the device_name used in pair begin.",
        ),
    ] = "vizio-smartcast CLI",
    output_format: FormatOption = None,
) -> None:
    """Cancel an in-progress pairing session."""
    fmt = _fmt(ctx, output_format)

    async def _go() -> None:
        async with Vizio(host=host, device_type=device_type) as v:
            await v.cancel_pair(device_id=device_id, device_name=device_name)

    try:
        asyncio.run(_go())
    except VizioError as e:
        _err.print(f"Pairing cancel failed: {e}")
        raise typer.Exit(code=1) from e

    _print(render_message("Cancel request sent", fmt=fmt))


@pair_app.command("interactive")
def pair_interactive(
    ctx: typer.Context,
    host: Annotated[str, typer.Argument(help="Device IP or IP:PORT.")],
    device_type: Annotated[
        DeviceType, typer.Option("--device-type", help="Device family.")
    ] = DeviceType.TV,
    device_id: Annotated[
        str,
        typer.Option("--device-id", help="Client device ID sent to the TV."),
    ] = "vizio-cli",
    device_name: Annotated[
        str,
        typer.Option("--device-name", help="Client device name sent to the TV."),
    ] = "vizio-smartcast CLI",
    save_as: Annotated[
        str | None,
        typer.Option("--save-as", help="Save the resulting alias for later."),
    ] = None,
    output_format: FormatOption = None,
) -> None:
    """Interactive pairing: opens a session, prompts for the PIN, returns the token."""
    state = _state(ctx)
    fmt = _fmt(ctx, output_format)

    async def _go() -> str:
        async with (
            Vizio(host=host, device_type=device_type) as v,
            v.pair_session(device_id=device_id, device_name=device_name) as session,
        ):
            _err.print(
                f"Pairing started — PIN should appear on the device. "
                f"(challenge_type={session.challenge.challenge_type})"
            )
            pin = typer.prompt("Enter PIN")
            return await session.complete(pin=pin)

    try:
        token = asyncio.run(_go())
    except VizioError as e:
        _err.print(f"Pairing failed: {e}")
        raise typer.Exit(code=1) from e

    if save_as:
        state.config.add_device(
            DeviceRecord(
                name=save_as,
                host=host,
                device_type=device_type,
                auth_token=token,
            )
        )
        state.config.save()
        _print(render_message(f"Paired and saved as {save_as!r}", fmt=fmt))
    elif fmt is OutputFormat.JSON:
        _print(render_value({"auth_token": token}, fmt=fmt))
    else:
        _print(render_rows([{"auth_token": token}], fmt=fmt))


# ---------------------------------------------------------------------------
# `vizio-smartcast power ...`
# ---------------------------------------------------------------------------

power_app = typer.Typer(name="power", help="Power control.")
app.add_typer(power_app)


@power_app.command("state")
def power_state(ctx: typer.Context) -> None:
    state = _state(ctx)
    on = _exec(ctx, lambda v: v.get_power_state())
    _print(render_value("on" if on else "off", fmt=state.output_format))


@power_app.command("on")
def power_on(ctx: typer.Context) -> None:
    _exec(ctx, lambda v: v.power_on())


@power_app.command("off")
def power_off(ctx: typer.Context) -> None:
    _exec(ctx, lambda v: v.power_off())


# ---------------------------------------------------------------------------
# `vizio-smartcast volume ...`
# ---------------------------------------------------------------------------

volume_app = typer.Typer(name="volume", help="Volume + mute control.")
app.add_typer(volume_app)


@volume_app.command("level")
def volume_level(ctx: typer.Context, output_format: FormatOption = None) -> None:
    level = _exec(ctx, lambda v: v.get_volume())
    _print(render_value(level, fmt=_fmt(ctx, output_format)))


@volume_app.command("up")
def volume_up(
    ctx: typer.Context, steps: Annotated[int, typer.Option("--steps")] = 1
) -> None:
    _exec(ctx, lambda v: v.volume_up(steps=steps))


@volume_app.command("down")
def volume_down(
    ctx: typer.Context, steps: Annotated[int, typer.Option("--steps")] = 1
) -> None:
    _exec(ctx, lambda v: v.volume_down(steps=steps))


@volume_app.command("mute")
def volume_mute(ctx: typer.Context) -> None:
    _exec(ctx, lambda v: v.mute())


@volume_app.command("unmute")
def volume_unmute(ctx: typer.Context) -> None:
    _exec(ctx, lambda v: v.unmute())


@volume_app.command("max")
def volume_max(ctx: typer.Context, output_format: FormatOption = None) -> None:
    target = _state(ctx).resolve()
    _print(
        render_value(
            target.device_type.profile.max_volume, fmt=_fmt(ctx, output_format)
        )
    )


# ---------------------------------------------------------------------------
# `vizio-smartcast input ...`
# ---------------------------------------------------------------------------

input_app = typer.Typer(name="input", help="Input control.")
app.add_typer(input_app)


@input_app.command("list")
def input_list(ctx: typer.Context, output_format: FormatOption = None) -> None:
    inputs = _exec(ctx, lambda v: v.get_inputs())
    rows = [
        {"name": i.name, "meta_name": i.meta_name, "current": i.is_current}
        for i in inputs
    ]
    _print(render_rows(rows, fmt=_fmt(ctx, output_format)))


@input_app.command("current")
def input_current(ctx: typer.Context, output_format: FormatOption = None) -> None:
    current = _exec(ctx, lambda v: v.get_current_input())
    _print(render_value(current, fmt=_fmt(ctx, output_format)))


@input_app.command("set")
def input_set(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Input name (e.g., HDMI-1).")],
) -> None:
    _exec(ctx, lambda v: v.set_input(name))


@input_app.command("next")
def input_next(ctx: typer.Context) -> None:
    _exec(ctx, lambda v: v.next_input())


# ---------------------------------------------------------------------------
# `vizio-smartcast remote ...`
# ---------------------------------------------------------------------------

remote_app = typer.Typer(name="remote", help="Remote key presses.")
app.add_typer(remote_app)


@remote_app.command("send")
def remote_send(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Remote key name (e.g., MENU).")],
) -> None:
    _exec(ctx, lambda v: v.send_key(key))


@remote_app.command("keys")
def remote_keys(ctx: typer.Context, output_format: FormatOption = None) -> None:
    target = _state(ctx).resolve()
    keys = sorted(target.device_type.profile.keymap.keys())
    _print(render_rows([{"key": k} for k in keys], fmt=_fmt(ctx, output_format)))


# ---------------------------------------------------------------------------
# `vizio settings ...`
# ---------------------------------------------------------------------------

settings_app = typer.Typer(name="settings", help="Read and write device settings.")
app.add_typer(settings_app)


@settings_app.command("types")
def settings_types(ctx: typer.Context, output_format: FormatOption = None) -> None:
    types = _exec(ctx, lambda v: v.get_setting_types())
    _print(render_rows([{"type": t} for t in types], fmt=_fmt(ctx, output_format)))


@settings_app.command("list")
def settings_list(
    ctx: typer.Context,
    setting_type: Annotated[str, typer.Argument(help="Category, e.g. 'audio'.")],
    output_format: FormatOption = None,
) -> None:
    settings = _exec(ctx, lambda v: v.get_settings(setting_type))
    rows = [
        {
            "name": info.name,
            "value": info.value,
            "type": info.type.value,
            "min": info.min,
            "max": info.max,
            "options": ",".join(info.options),
        }
        for info in settings.values()
    ]
    _print(render_rows(rows, fmt=_fmt(ctx, output_format)))


@settings_app.command("get")
def settings_get(
    ctx: typer.Context,
    setting_type: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    output_format: FormatOption = None,
) -> None:
    info = _exec(ctx, lambda v: v.get_setting(setting_type, name))
    _print(render_value(info.value, fmt=_fmt(ctx, output_format)))


@settings_app.command("set")
def settings_set(
    ctx: typer.Context,
    setting_type: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Argument()],
    value: Annotated[
        str,
        typer.Argument(help="New value (numeric strings are coerced to int)."),
    ],
) -> None:
    typed_value: int | str
    try:
        typed_value = int(value)
    except ValueError:
        typed_value = value
    _exec(ctx, lambda v: v.set_setting(setting_type, name, typed_value))


# ---------------------------------------------------------------------------
# `vizio-smartcast app ...`
# ---------------------------------------------------------------------------

apps_app = typer.Typer(name="app", help="SmartCast app control.")
app.add_typer(apps_app)


@apps_app.command("current")
def app_current(ctx: typer.Context, output_format: FormatOption = None) -> None:
    name = _exec(ctx, lambda v: v.get_current_app())
    _print(render_value(name or "(no app running)", fmt=_fmt(ctx, output_format)))


@apps_app.command("launch")
def app_launch(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="App name (case-insensitive).")],
) -> None:
    _exec(ctx, lambda v: v.launch_app(name))


@apps_app.command("launch-config")
def app_launch_config(
    ctx: typer.Context,
    app_id: Annotated[str, typer.Argument()],
    name_space: Annotated[int, typer.Argument()],
    message: Annotated[str | None, typer.Argument()] = None,
) -> None:
    _exec(
        ctx,
        lambda v: v.launch_app_config(
            AppConfig(app_id=app_id, name_space=name_space, message=message)
        ),
    )


# ---------------------------------------------------------------------------
# `vizio-smartcast info ...`
# ---------------------------------------------------------------------------

info_app = typer.Typer(name="info", help="Device identity.")
app.add_typer(info_app)


@info_app.command("model")
def info_model(ctx: typer.Context, output_format: FormatOption = None) -> None:
    _print(
        render_value(
            _exec(ctx, lambda v: v.get_model_name()), fmt=_fmt(ctx, output_format)
        )
    )


@info_app.command("serial")
def info_serial(ctx: typer.Context, output_format: FormatOption = None) -> None:
    _print(
        render_value(
            _exec(ctx, lambda v: v.get_serial_number()), fmt=_fmt(ctx, output_format)
        )
    )


@info_app.command("esn")
def info_esn(ctx: typer.Context, output_format: FormatOption = None) -> None:
    _print(
        render_value(_exec(ctx, lambda v: v.get_esn()), fmt=_fmt(ctx, output_format))
    )


@info_app.command("version")
def info_version(ctx: typer.Context, output_format: FormatOption = None) -> None:
    _print(
        render_value(
            _exec(ctx, lambda v: v.get_version()), fmt=_fmt(ctx, output_format)
        )
    )


@info_app.command("all")
def info_all(ctx: typer.Context, output_format: FormatOption = None) -> None:
    info = _exec(ctx, lambda v: v.get_device_info())
    _print(
        render_rows(
            [
                {
                    "model": info.model,
                    "serial_number": info.serial_number,
                    "esn": info.esn,
                    "version": info.version,
                    "inputs": ",".join(i.name for i in info.inputs),
                }
            ],
            fmt=_fmt(ctx, output_format),
        )
    )


# ---------------------------------------------------------------------------
# `vizio battery ...` (Crave only)
# ---------------------------------------------------------------------------

battery_app = typer.Typer(name="battery", help="Battery (Crave devices only).")
app.add_typer(battery_app)


@battery_app.command("level")
def battery_level(ctx: typer.Context, output_format: FormatOption = None) -> None:
    _print(
        render_value(
            _exec(ctx, lambda v: v.get_battery_level()), fmt=_fmt(ctx, output_format)
        )
    )


@battery_app.command("charging")
def battery_charging(ctx: typer.Context, output_format: FormatOption = None) -> None:
    status = _exec(ctx, lambda v: v.get_charging_status())
    _print(render_value(status.name.lower(), fmt=_fmt(ctx, output_format)))


__all__ = ["app"]
