"""`rhine-gateway`: serve chat-app connectors. Kept apart from the `rhine` CLI on purpose."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from ..config import ConfigError, ProviderConfig, TaskConfig, load_config
from .server import Gateway

app = typer.Typer(add_completion=False, help="连接器服务端：让聊天软件连接器通过 WebSocket 接入角色")


@app.command()
def main(
    config: Annotated[Path, typer.Option("-c", "--config", help="配置文件")] = Path("config.yaml"),
    host: Annotated[str, typer.Option(help="监听地址；只接受本机和 tailnet 的连接")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="端口")] = 8791,
    token_file: Annotated[Path | None, typer.Option(envvar="RHINE_GATEWAY_TOKEN_FILE", help="连接器用的 token 文件（600 权限）")] = None,
    debounce: Annotated[float, typer.Option(help="私聊和没有筛选的群聊等多少秒再回应；有筛选的群由筛选决定等待时间")] = 2.5,
    echo: Annotated[bool, typer.Option("--echo", help="测试模式：不调模型，被 @ 时原样回显；数据另存")] = False,
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="输出调试日志")] = False,
) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    path = token_file.expanduser() if token_file is not None else Path(config_home) / "ohmyrhine" / "gateway.token"
    if not path.is_file():
        raise typer.BadParameter(f"找不到 token 文件 {path}", param_hint="--token-file")
    if path.stat().st_mode & 0o077:
        raise typer.BadParameter(f"{path} 的权限太宽，请 chmod 600", param_hint="--token-file")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise typer.BadParameter(f"{path} 是空的", param_hint="--token-file")

    try:
        cfg = load_config(config)
    except ConfigError as e:
        raise typer.BadParameter(str(e), param_hint="--config") from None
    if echo:
        cfg.providers["echo"] = ProviderConfig(type="echo")
        cfg.tasks = {"roleplay": TaskConfig(provider="echo")}
        cfg.characters = {}
        cfg.data_dir = cfg.data_dir / "gateway-echo"
    asyncio.run(Gateway(cfg, token, debounce_s=debounce).serve(host, port))


if __name__ == "__main__":
    app()
