#!/usr/bin/env -S python3 -u
#
# A quick-and-dirty Swaybar server in Python.
# Copyright (C) 2025 Marcin Słowik
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from __future__ import annotations

import abc
import asyncio
import enum
import json
import psutil
import subprocess
import sys
import time

from collections import defaultdict
from dataclasses import dataclass, fields
from functools import lru_cache
from typing import Any, Callable, Generator, Iterable, Iterator, Optional, Self, Sequence, Type, TypeVar

T = TypeVar('T')


## Configuration
# By default we use the preconfigured sway font, but for monospace needs (like stacked strings) we
# need a configured monospace font.
font_monospace = 'RobotoMonoNerdFont'
# Normal icon width relative to the swaybar height, this might be font-dependent
icon_rel_width = 19.5/27
DEFAULT_debug_mode_in = 10


## Runtime Configuration
debug_mode = False
debug_mode_in = DEFAULT_debug_mode_in


## Constant definitions
nf_md_volume_off = '\U000f0581'
nf_md_volume_low = '\U000f057f'
nf_md_volume_medium = '\U000f0580'
nf_md_volume_high = '\U000f057e'

nf_md_arrow_up_thick = '\U000f005e'
nf_md_arrow_down_thick = '\U000f0046'
nf_md_arrow_collapse_down = '\U000f0792'
nf_md_arrow_collapse_left = '\U000f0793'
nf_md_arrow_collapse_right = '\U000f0794'
nf_md_arrow_collapse_up = '\U000f0795'

nf_md_vpn = '\U000f0582'

nf_cod_triangle_down = '\ueb6e'
nf_cod_triangle_left = '\ueb6f'
nf_cod_triangle_right = '\ueb70'
nf_cod_triangle_up = '\ueb71'

nf_fa_globe = '\uf0ac'
nf_fa_desktop = '\uf108'
nf_fa_network_wired = '\uef09'
nf_fa_wifi = '\uf1eb'
nf_fa_docker = '\uf21f'

ev_click_lmb = 272
ev_click_rmb = 273
ev_click_mmb = 274
ev_click_prev = 275
ev_click_next = 276
ev_click_btn6 = 277
ev_scroll_up = 768
ev_scroll_down = 769
ev_scroll_right = 770
ev_scroll_left = 771


## Generic formatters
def fmt_stacked(text1: str, text2: str):
    """
    Highly experimental, not really reliable
    """
    t1l = len(text1)
    t2l = len(text2)
    fs = 7*1024
    rise = 4096
    sp = int(1.6 * fs)
    nbsp = '\u00a0'
    padding = "" if t2l > t1l else nbsp*(t1l-t2l)
    return f'<span font="{font_monospace}"><span rise="{rise*3//2}" font_size="{fs}">{text1}</span><span letter_spacing="-{sp}" font_size="{fs}">{nbsp*t1l}</span><span rise="-{rise//2}" font_size="{fs}">{text2}{padding}</span></span>'


def fmt_icon(if_name: str):
    if if_name == 'lo':
        if_icon = nf_fa_desktop
    elif if_name.startswith('wlan'):
        sfx = if_name.removeprefix('wlan')
        if_icon = f"{nf_fa_wifi} <sub>{sfx}</sub>"
    elif if_name.startswith('tailscale'):
        sfx = if_name.removeprefix('tailscale')
        if_icon = f"{nf_md_vpn} <sub>{sfx}</sub>"
    elif if_name.startswith('en'):
        sfx = if_name.removeprefix('en')
        if_icon = f"{nf_fa_network_wired} <sub>{sfx}</sub>"
    elif if_name == '*':
        if_icon = f"{nf_fa_globe} "
    elif if_name.startswith('docker'):
        sfx = if_name.removeprefix('docker')
        if_icon = f"{nf_fa_docker} <sub>{sfx}</sub>"
    elif if_name.startswith('veth'):  # TODO: regexp with hex
        sfx = if_name.removeprefix('veth')[:2]
        if_icon = f"{nf_fa_docker} <sub>{sfx}</sub>"
    elif if_name.startswith('br-'):  # TODO: regexp with hex
        sfx = if_name.removeprefix('br-')[:2]
        sfx = fmt_stacked('br', sfx)
        if_icon = f"{nf_fa_docker} {sfx}"
    else:
        if_icon = f"{nf_fa_globe} <sub>{if_name}</sub>"
    return if_icon


def fmt_value(value: float, unit: str, binary: bool):
    value, unitpfx = autounit(value, binary)
    # Fix for binary units under 1024
    if value > 1000:
        return f"{int(round(value))}{unitpfx}{unit}"
    else:
        return f"{value:.3g}{unitpfx}{unit}"


icon_triangle_up_down = fmt_stacked(nf_cod_triangle_up, nf_cod_triangle_down)


## Helper functions
def autounit(x: float, binary=True) -> tuple[float, str]:
    if binary:
        if x < 1024: 
            return x, ""
        x /= 1024
        if x < 1024:
            return x, "Ki"
        x /= 1024
        if x < 1024:
            return x, "Mi"
        x /= 1024
        if x < 1024:
            return x, "Gi"
        x /= 1024
        return x, "Ti"
    else:
        if x < 1000: 
            return x, ""
        x /= 1000
        if x < 1000:
            return x, "k"
        x /= 1000
        if x < 1000:
            return x, "M"
        x /= 1000
        if x < 1000:
            return x, "G"
        x /= 1000
        return x, "T"


async def create_file_reader(file, limit=65536):
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader(limit=limit, loop=loop)
    protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
    await loop.connect_read_pipe(lambda: protocol, file)
    return reader


async def sub_call(*args):
    proc = await asyncio.create_subprocess_exec(*args)
    await proc.communicate()


def eprint(*args):
    print("ERROR:", *args, file=sys.stderr)


@lru_cache(maxsize=None)
def field_names(dataclass: Type):
    return { field.name for field in fields(dataclass) }


async def json_stream_reader(
    reader: asyncio.StreamReader,
    buffer_limit: int = 65536,
    decode_grace: int = 100,
    array_format: bool = False,
    json_decoder = json.JSONDecoder()
):
    buffer = ''
    default_grace = decode_grace
    first_element = True

    while not reader.at_eof():
        curr_buffer = await reader.read(buffer_limit)
        buffer += curr_buffer.decode()
        try:
            while buffer:
                tbuffer = buffer.lstrip()
                # In array format, skip the first array opening or subsequent commas between objects
                if array_format:
                    assert tbuffer[0] == '[' if first_element else ','
                    tbuffer = tbuffer[1:].lstrip()
                payload, end = json_decoder.raw_decode(tbuffer)
                first_element = False
                buffer = tbuffer[end:].lstrip()
                yield payload
                decode_grace = default_grace
        except json.JSONDecodeError:
            # eprint(f"{self.__class__} {e=} {self.buffer=} {self.decode_grace=}")
            decode_grace -= 1
            buffer = buffer.lstrip()  # lstrip should always be safe
            if not decode_grace:
                raise


class RepeatingIterator(Iterable[T]):
    def  __init__(self, func: Callable[[], Generator[T, None, None]]):
        self.func = func

    def __iter__(self) -> Iterator[T]:
        return self.func()


def repeating(func: Callable[[], Generator[T, None, None]]) -> Iterable[T]:
    return RepeatingIterator(func)


## Monitors
class PipeWireMonitor:
    @dataclass
    class NodeProps:
        id: int
        name: str
        info: dict

        media_class: str

        volume: Optional[float] = None
        is_muted: Optional[bool] = None

        @classmethod
        def from_info(cls, id, info: dict):
            name = info['props']['node.name']
            media_class = info['props'].get('media.class', None)
            volume = None
            is_muted = None
            try:
                props = [p for p in info['params']['Props'] if 'volume' in p]
                if not props:
                    eprint(f"missing volume props for node {id}")
                else:
                    volume = props[0]['volume']
                    channelVolumes = props[0]['channelVolumes']
                    if channelVolumes:
                        volume *= sum(channelVolumes) / len(channelVolumes)
                    volume = round(pow(volume, 1/3), 2)
                    is_muted = props[0]['mute']
            except KeyError:
                ...
            return cls(id, name, info, media_class, volume, is_muted)

    metadata: dict[str, dict[str, Any]]
    devices: dict[int, Any]
    clients: dict[int, Any]
    nodes: dict[int, NodeProps]
    nodes_by_name: dict[str, NodeProps]
    ports: dict[int, Any]
    links: dict[int, Any]

    def __init__(self):
        self.task = asyncio.create_task(self.read_forever())
        self.metadata = defaultdict(dict)
        self.devices = {}
        self.clients = {}
        self.nodes = {}
        self.nodes_by_name = {}
        self.ports = {}
        self.links = {}
        # ---
        self.event = asyncio.Event()

    def notify_change(self):
        self.event.set()

    def try_delete_node(self, id: int):
        node = self.nodes.pop(id, None)
        if node is not None:
            del self.nodes_by_name[node.name]
            return True
        else:
            return False

    def try_delete_device(self, id: int):
        device = self.devices.pop(id, None)
        return device is not None

    def process_update(self, update: list[dict[str, Any]]):
        for entry in update:
            match entry:
                case {'id': id, 'info': None}:
                    eprint(f"Delete node/device {id}")
                    self.try_delete_node(id) or self.try_delete_device(id)

                case {'id': id, 'type': "PipeWire:Interface:Metadata", 'props': props, 'metadata': metadata}:
                    eprint(f"Add metadata {props['metadata.name']}")
                    self.metadata[props['metadata.name']].update(
                        {m['key']: m['value'] for m in metadata}
                    )

                case {'id': id, 'type': "PipeWire:Interface:Device", 'info': info}:
                    eprint(f"Add/update device {id}")
                    self.devices[id] = info

                case {'id': id, 'type': "PipeWire:Interface:Client", 'info': info}:
                    eprint(f"Add/update client {id}")
                    self.clients[id] = info

                case {'id': id, 'type': "PipeWire:Interface:Node", 'info': info}:
                    eprint(f"Add/update node {id}")
                    node = self.NodeProps.from_info(id, info)
                    self.nodes[id] = node
                    self.nodes_by_name[node.name] = node

                case {'id': id, 'type': "PipeWire:Interface:Port", 'info': info}:
                    eprint(f"Add/update port {id}")
                    self.ports[id] = info

                case {'id': id, 'type': "PipeWire:Interface:Link", 'info': info}:
                    eprint(f"Add/update link {id}")
                    self.links[id] = info

                case _:
                    eprint(f"Other: {entry}"[:512], "...")
                    pass
        self.notify_change()

    @property
    def default_audio_source(self):
        try:
            node_id = self.metadata['default']['default.audio.source']['name']
            node = self.nodes_by_name[node_id]
            return node
        except KeyError:
            return None
    
    @property
    def default_audio_sink(self):
        try:
            node_id = self.metadata['default']['default.audio.sink']['name']
            node = self.nodes_by_name[node_id]
            return node
        except KeyError:
            return None

    @property
    def sources(self):
        yield from (node for node in self.nodes.values() if node.media_class == 'Audio/Source')

    @property
    def sinks(self):
        yield from (node for node in self.nodes.values() if node.media_class == 'Audio/Sinks')

    async def read_forever(self):
        self.proc = await asyncio.create_subprocess_exec("pw-dump", "-m", stdout=subprocess.PIPE)
        stdout = self.proc.stdout
        assert stdout is not None
        async for update in json_stream_reader(stdout):
            self.process_update(update)

    async def close(self):
        self.proc.terminate()
        await self.task


class NetworkMonitor:
    @dataclass
    class NetStats:
        is_up: bool = False
        bytes_sent: int = 0
        bytes_recv: int = 0
        packets_sent: int = 0
        packets_recv: int = 0
        last_update: float = 0
        bytes_sent_rate: float = 0
        bytes_recv_rate: float = 0
        packets_sent_rate: float = 0
        packets_recv_rate: float = 0

    interfaces: dict[str, NetStats]

    def __init__(self):
        NetStats = self.NetStats
        self.interfaces = defaultdict(NetStats)

    def update(self):
        net_stats = psutil.net_if_stats()
        net_ioctr = psutil.net_io_counters(pernic=True, nowrap=True)
        last_update = time.perf_counter()
        cumst = self.interfaces['*']
        cumst.last_update = last_update
        cumst.bytes_sent = cumst.bytes_sent_rate = cumst.packets_sent = cumst.packets_sent_rate = 0
        cumst.bytes_recv = cumst.bytes_recv_rate = cumst.packets_recv = cumst.packets_recv_rate = 0
        for if_name, if_stats in net_stats.items():
            ioctrs = net_ioctr[if_name]
            iface = self.interfaces[if_name]
            iface.is_up = if_stats.isup
            if iface.last_update:
                bytes_sent_delta = ioctrs.bytes_sent - iface.bytes_sent
                bytes_recv_delta = ioctrs.bytes_recv - iface.bytes_recv
                packets_sent_delta = ioctrs.packets_sent - iface.packets_sent
                packets_recv_delta = ioctrs.packets_recv - iface.packets_recv
                last_update_delta = last_update - iface.last_update
                iface.bytes_sent_rate = bytes_sent_delta / last_update_delta
                iface.bytes_recv_rate = bytes_recv_delta / last_update_delta
                iface.packets_sent_rate = packets_sent_delta / last_update_delta
                iface.packets_recv_rate = packets_recv_delta / last_update_delta
                cumst.bytes_sent_rate += iface.bytes_sent_rate
                cumst.bytes_recv_rate += iface.bytes_recv_rate
                cumst.packets_sent_rate += iface.packets_sent_rate
                cumst.packets_recv_rate += iface.packets_recv_rate
            iface.bytes_sent = ioctrs.bytes_sent
            iface.bytes_recv = ioctrs.bytes_recv
            iface.packets_sent = ioctrs.packets_sent
            iface.packets_recv = ioctrs.packets_recv
            cumst.bytes_sent += iface.bytes_sent
            cumst.bytes_recv += iface.bytes_recv
            cumst.packets_sent += iface.packets_sent
            cumst.packets_recv += iface.packets_recv
            iface.last_update = last_update


## WIP: Blocks
@dataclass(frozen=True)
class ClickEvent:
    name: str
    button: int
    event: int
    relative_x: int
    relative_y: int
    width: int
    height: int
    instance: Optional[str] = None
    scale: Optional[float] = None

    @classmethod
    def from_event(cls, event: dict[str,Any]) -> Self:
        return cls(**{k:v for k,v in event.items()
            if k in field_names(cls)})


class BlockBase(abc.ABC):
    @property
    @abc.abstractmethod
    def rendered(self) -> dict[str, str]:
        ...

    @abc.abstractmethod
    def onclick(self, event: ClickEvent):
        ...


class BlockControllerBase(abc.ABC):
    @abc.abstractmethod
    async def loop(self, notify_update: Callable[[], Any]):
        ...


class BlockCollectionBase(abc.ABC):
    @property
    @abc.abstractmethod
    def blocks(self) -> Iterable[BlockBase]:
        ...


class ClockBlock(BlockBase, BlockControllerBase):
    def __init__(self):
        self._rendered = {"name": "clock", "full_text": "(startup)"}

    @property
    def rendered(self):
        return self._rendered

    def onclick(self, event: ClickEvent):
        global debug_mode, debug_mode_in
        if not debug_mode and debug_mode_in <= 0:
            debug_mode = True
        debug_mode_in -= 1

    def update(self):
        fmt_now = time.strftime("%Y-%m-%d %H:%M:%S")
        self._rendered['full_text'] = fmt_now

    async def loop(self, notify_update: Callable[[], Any]):
        N = 599
        while True:
            # First time each N seconds sleep less than 1 second to align with the wall clock
            self.update()
            notify_update()
            adj = time.clock_gettime(time.CLOCK_REALTIME) % 1
            await asyncio.sleep(1 - adj)
            for _ in range(N):
                self.update()
                notify_update()
                await asyncio.sleep(1)


class AudioBlock(BlockBase, BlockControllerBase):
    def __init__(self, pwm: PipeWireMonitor, id=None):
        self._rendered = {"name": "audio", "full_text": "(startup)"}
        self.pwm = pwm
        self.node_id = id

    @property
    def rendered(self) -> dict[str, str]:
        return self._rendered

    def onclick(self, event: ClickEvent):
        if event.event == ev_click_lmb:
            act_toggle_mute(self.node_id)
        elif event.event == ev_scroll_up:
            act_volume_up(self.node_id)
        elif event.event == ev_scroll_down:
            act_volume_down(self.node_id)

    def update(self):
        if self.node_id is not None:
            node = self.pwm.nodes[self.node_id]
        else:
            node = self.pwm.default_audio_sink
        if node is None or node.volume is None:
            rendered = "(no audio)"
        else:
            eprint(f"audio update for node: {node.id} {node.name} {node.volume} {node.is_muted} {node.media_class} {node.info['params']['Props']=}"[:1024], "...")
            volume, is_muted = node.volume, node.is_muted
            if is_muted:
                icon = nf_md_volume_off
            elif volume < 0.3:
                icon = nf_md_volume_low
            elif volume < 0.6:
                icon = nf_md_volume_medium
            else:
                icon = nf_md_volume_high
            rendered = f"{icon} {volume:.0%}"

        self._rendered["full_text"] = rendered

    async def loop(self, notify_update: Callable[[], Any]):
        pwm_event = self.pwm.event
        while True:
            await pwm_event.wait()
            pwm_event.clear()
            self.update()
            notify_update()


class NetworkMetric(enum.IntEnum):
    Bytes = 0
    ByteRate = 1
    Packets = 2
    PacketRate = 3

    def next(self) -> Self:
        next = (self.value + 1) % len(self.__class__)
        return self.__class__(next)


class NetworkBlockControllerMode(enum.IntEnum):
    Cumulative = 0
    All = 1
    Highest = 2


class NetworkBlockController(BlockControllerBase, BlockCollectionBase):
    _cum_block: NetworkBlock
    _all_blocks: Sequence[NetworkBlock]
    _all_block_names: Any | None

    def __init__(self, nm: NetworkMonitor):
        self.nm = nm
        self.metric = NetworkMetric.Bytes
        self.mode = NetworkBlockControllerMode.All
        self._cum_block = NetworkBlock('*', nm.interfaces['*'], self)
        self._all_blocks = []
        self._all_block_names = None

    @property
    def blocks(self) -> Iterator[NetworkBlock]:
        match(self.mode):
            case NetworkBlockControllerMode.Cumulative:
                yield self._cum_block
            case NetworkBlockControllerMode.All:
                yield from self._all_blocks
            case NetworkBlockControllerMode.Highest:
                yield self._cum_block

    def update(self):
        self.nm.update()
        # Update _all_blocks if the observed interfaces changed
        block_names = [k for k,v in self.nm.interfaces.items() if v.is_up]
        # The order in which the names appear should not have changed since dict keys are stable
        if block_names != self._all_block_names:
            # Need to recreate _all_blocks, cheapest way is from scratch
            eprint(f"Change in observed interface list: {self._all_block_names} -> {block_names}")
            nblocks = [
                NetworkBlock(if_name, if_stats, self)
                for if_name, if_stats in self.nm.interfaces.items()
                if if_stats.is_up and if_name != '*'
            ] + [self._cum_block]
            self._all_blocks = nblocks
            self._all_block_names = block_names
        for block in self._all_blocks:
            block.update(self.metric)

    async def loop(self, notify_update: Callable[[], Any]):
        while True:
            self.update()
            # No notify_update needed -- will be rendered with the next clock
            await asyncio.sleep(5)


class NetworkBlock(BlockBase):
    def __init__(
        self,
        if_name: str,
        stats: NetworkMonitor.NetStats,
        nc: NetworkBlockController
    ):
        self._rendered = {"name": "network", "instance": if_name, "full_text": "(startup)", "markup": "pango"}
        self.if_name = if_name
        self.if_icon = fmt_icon(if_name)
        self.stats = stats
        self.nc = nc

    @property
    def rendered(self) -> dict[str, str]:
        return self._rendered

    def onclick(self, event: ClickEvent):
        self.nc.metric = self.nc.metric.next()
        self.nc.update()

    def _metrics(self, metric: NetworkMetric):
        match metric:
            case NetworkMetric.Bytes:
                recv = self.stats.bytes_recv
                sent = self.stats.bytes_sent
                unit = "B"
                binary = True
            case NetworkMetric.ByteRate:
                recv = self.stats.bytes_recv_rate
                sent = self.stats.bytes_sent_rate
                unit = "B/s"
                binary = True
            case NetworkMetric.Packets:
                recv = self.stats.packets_recv
                sent = self.stats.packets_sent
                unit = "p"
                binary = False
            case NetworkMetric.PacketRate:
                recv = self.stats.packets_recv_rate
                sent = self.stats.packets_sent_rate
                unit = "p/s"
                binary = False
        return recv, sent, unit, binary

    def update(self, metric: NetworkMetric):
        recv, sent, unit, binary = self._metrics(metric)
        fmt_send = f"{nf_md_arrow_up_thick} {fmt_value(sent, unit, binary)}"
        fmt_recv = f"{nf_md_arrow_down_thick} {fmt_value(recv, unit, binary)}"
        rendered = f"{self.if_icon} {fmt_stacked(fmt_send, fmt_recv)}"
        self._rendered["full_text"] = rendered


class DebugBlock(BlockBase):
    def __init__(self):
        self._rendered = {"name": "debug", "full_text": "(startup)", "markup": "pango"}

    @property
    def rendered(self):
        if debug_mode:
            return self._rendered
        else:
            return {}

    def onclick(self, event: ClickEvent):
        global debug_mode, debug_mode_in
        icon_width = event.height * icon_rel_width
        if event.relative_x < icon_width:
            debug_mode = False
            debug_mode_in = DEFAULT_debug_mode_in

    def _rerender(self, message):
        self._rendered['full_text'] = f"{nf_cod_triangle_right} {message}"

    def record_event(self, event: ClickEvent):
        self._event = f"{event}"
        self._rerender(self._event)

    def record_error(self, error):
        eprint(error)
        self._error = f"{error=}"
        self._rerender(self._error)


class InputController(BlockControllerBase):
    def __init__(self, blocks: Iterable[BlockBase], debug_block: DebugBlock):
        self.blocks = blocks
        self.debug_block = debug_block

    def update(self, event: dict[str, str | int | float], notify: Callable[[], Any]):
        try:
            nevent = ClickEvent.from_event(event)
            self.debug_block.record_event(nevent)
            for block in self.blocks:
                rendered = block.rendered
                name, instance = rendered.get('name', None), rendered.get('instance', None)
                if name == nevent.name and instance == nevent.instance:
                    block.onclick(nevent)
                    break
            else:
                eprint(f"Unhandled input event: {nevent}")
            notify()
        except Exception as e:
            self.debug_block.record_error(e)

    async def loop(self, notify_update: Callable[[], Any]):
        reader = await create_file_reader(sys.stdin)
        async for event in json_stream_reader(reader, array_format=True):
            self.update(event, notify_update)


## Action definitions
def act_toggle_mute(node_id: Optional[int] = None):
    node = "@DEFAULT_AUDIO_SINK@" if node_id is None else str(node_id)
    asyncio.create_task(sub_call("wpctl", "set-mute", node, "toggle"))

def act_volume_up(node_id: Optional[int] = None):
    node = "@DEFAULT_AUDIO_SINK@" if node_id is None else str(node_id)
    asyncio.create_task(sub_call("wpctl", "set-volume", node, "1%+"))

def act_volume_down(node_id: Optional[int] = None):
    node = "@DEFAULT_AUDIO_SINK@" if node_id is None else str(node_id)
    asyncio.create_task(sub_call("wpctl", "set-volume", node, "1%-"))


## Main loop(s)
async def main():
    pwm = PipeWireMonitor()
    nm = NetworkMonitor()

    update_event = asyncio.Event()
    def notify():
        update_event.set()

    clock_block = ClockBlock()
    audio_block = AudioBlock(pwm)
    network_controller = NetworkBlockController(nm)
    debug_block = DebugBlock()

    @repeating
    def blocks():
        yield from network_controller.blocks
        yield audio_block
        yield clock_block
        yield debug_block

    input_controller = InputController(blocks, debug_block)

    controllers = [
        clock_block,
        audio_block,
        network_controller,
        input_controller,
    ]

    updaters = [
        asyncio.create_task(controller.loop(notify))
        for controller in controllers
    ]

    try:
        print('{"version":1,"click_events":true,"cont_signal":18,"stop_signal":19}\n[')
        while True:
            try:
                await update_event.wait()
                update_event.clear()
                msg = [block.rendered for block in blocks]
                msg = json.dumps(msg)
                print(f'{msg},')
            except Exception as e:
                eprint(f"in main loop: {e!r}")
    finally:
        for task in updaters:
            task.cancel()
        await pwm.close()

if __name__ == '__main__':
    asyncio.run(main())

