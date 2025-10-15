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
from typing import Any, Callable, Optional, Self, Type

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

nf_cod_triangle_left = '\ueb6f'
nf_cod_triangle_right = '\ueb70'

nf_fa_globe = '\uf0ac'
nf_fa_desktop = '\uf108'
nf_fa_network_wired = '\uef09'
nf_fa_wifi = '\uf1eb'

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


class JSONStreamInput:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        callback: Callable[[Any], None],
        buffer_limit: int = 65536,
        decode_grace: int = 100,
        array_format: bool = False,
        json_decoder = json.JSONDecoder()
    ):
        self.reader = reader
        self.callback = callback
        self.buffer = ''
        self.buffer_limit = buffer_limit
        self.json_decoder = json_decoder
        self.default_grace = decode_grace
        self.decode_grace: int  # set to default in process_buffer
        self.array_format = array_format

    def process_buffer(self):
        self.decode_grace = self.default_grace
        try:
            while self.buffer:
                buffer = self.buffer.lstrip()
                # In array format, skip the first array opening or subsequent commas between objects
                if self.array_format:
                    # TODO: if this works long-term, change the check to swap
                    # between the first and consecutive times
                    assert buffer[0] in '[,'
                    buffer = buffer[1:].lstrip()
                payload, end = self.json_decoder.raw_decode(buffer)
                self.decode_grace = self.default_grace
                self.buffer = buffer[end:].lstrip()
                self.callback(payload)
        except json.JSONDecodeError:
            # eprint(f"{self.__class__} {e=} {self.buffer=} {self.decode_grace=}")
            self.decode_grace -= 1
            self.buffer = self.buffer.lstrip()  # lstrip should always be safe
            if not self.decode_grace:
                raise

    async def read_forever(self):
        while not self.reader.at_eof():
            curr_buffer = await self.reader.read(self.buffer_limit)
            self.buffer += curr_buffer.decode()
            self.process_buffer()


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
                    volume *= sum(channelVolumes) / len(channelVolumes)
                    volume = round(pow(volume, 1/3), 2)
                    is_muted = props[0]['mute']
            except KeyError:
                ...
            # eprint(f"from_info: {id=} {name=} {volume=} {is_muted=}")
            return cls(id, name, info, media_class, volume, is_muted)

    nodes: dict[int, NodeProps]
    nodes_by_name: dict[str, NodeProps]
    devices: dict[int, Any]
    metadata: dict[str, dict[str, Any]]

    def __init__(self):
        self.task = asyncio.create_task(self.read_forever())
        self.nodes = {}
        self.nodes_by_name = {}
        self.devices = {}
        self.metadata = defaultdict(dict)
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
        # NOTE: all notifies in this function should trigger at the same time
        # since this is not an async function and it doesn't have breaking points
        for entry in update:
            match entry:
                case {'id': id, 'info': None}:
                    self.try_delete_node(id) or self.try_delete_device(id)

                case {'id': id, 'type': "PipeWire:Interface:Metadata", 'props': props, 'metadata': metadata}:
                    self.metadata[props['metadata.name']].update(
                        {m['key']: m['value'] for m in metadata}
                    )
                    self.notify_change()

                case {'id': id, 'type': "PipeWire:Interface:Node", 'info': info}:
                    node = self.NodeProps.from_info(id, info)
                    self.nodes[id] = node
                    self.nodes_by_name[node.name] = node
                    self.notify_change()

                case {'id': id, 'type': "PipeWire:Interface:Device", 'info': info}:
                    self.devices[id] = info

                case _:
                    pass

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
        json_stream_input = JSONStreamInput(stdout, self.process_update)
        await json_stream_input.read_forever()

    async def close(self):
        self.proc.terminate()
        await self.task


class NetworkMonitor:
    @dataclass
    class IfStats:
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

    interfaces: dict[str, IfStats]

    def __init__(self):
        IfStats = self.IfStats
        self.interfaces = defaultdict(lambda: IfStats())

    def update(self):
        net_stats = psutil.net_if_stats()
        net_ioctr = psutil.net_io_counters(pernic=True, nowrap=True)
        for if_name, if_stats in net_stats.items():
            ioctrs = net_ioctr[if_name]
            iface = self.interfaces[if_name]
            iface.is_up = if_stats.isup
            last_update = time.perf_counter()
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
            iface.bytes_sent = ioctrs.bytes_sent
            iface.bytes_recv = ioctrs.bytes_recv
            iface.packets_sent = ioctrs.packets_sent
            iface.packets_recv = ioctrs.packets_recv
            iface.last_update = last_update


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


class NetworkBlock(BlockBase, BlockControllerBase):
    def __init__(self, nm):
        self._rendered = {"name": "network", "full_text": "(startup)", "markup": "pango"}
        self.nm = nm
        self.metric = NetworkMetric.Bytes

    @property
    def rendered(self) -> dict[str, str]:
        return self._rendered

    def onclick(self, event: ClickEvent):
        self.metric = self.metric.next()
        self.update()

    @classmethod
    def _fmt_icon(cls, if_name):
        if if_name == 'lo':
            if_icon = nf_fa_desktop
        elif if_name.startswith('wlan'):
            sfx = if_name.removeprefix('wlan')
            if_icon = f"{nf_fa_wifi} <sub>{sfx}</sub>"
        elif if_name.startswith('tailscale'):
            sfx = if_name.removeprefix('tailscale')
            if_icon = f"{nf_fa_globe} <sub>{sfx}</sub>"
        elif if_name.startswith('en'):
            sfx = if_name.removeprefix('en')
            if_icon = f"{nf_fa_network_wired} <sub>{sfx}</sub>"
        else:
            if_icon = if_name
        return if_icon

    def _metrics(self, iface):
        match self.metric:
            case NetworkMetric.Bytes:
                recv = iface.bytes_recv
                sent = iface.bytes_sent
                unit = "B"
                binary = True
            case NetworkMetric.ByteRate:
                recv = iface.bytes_recv_rate
                sent = iface.bytes_sent_rate
                unit = "B/s"
                binary = True
            case NetworkMetric.Packets:
                recv = iface.packets_recv
                sent = iface.packets_sent
                unit = "p"
                binary = False
            case NetworkMetric.PacketRate:
                recv = iface.packets_recv_rate
                sent = iface.packets_sent_rate
                unit = "p/s"
                binary = False
        return recv, sent, unit, binary

    @classmethod
    def _fmt_value(cls, value: float, unit: str, binary: bool):
        value, unitpfx = autounit(value, binary)
        # Fix for binary units under 1024
        if value > 1000:
            return f"{int(round(value))}{unitpfx}{unit}"
        else:
            return f"{value:.3g}{unitpfx}{unit}"

    def update(self):
        self.nm.update()
        all_ifaces = []
        for if_name, iface in self.nm.interfaces.items():
            if iface.is_up:
                recv, sent, unit, binary = self._metrics(iface)
                if_icon = self._fmt_icon(if_name)
                fmt_send = f"{nf_md_arrow_up_thick} {self._fmt_value(sent, unit, binary)}"
                fmt_recv = f"{nf_md_arrow_down_thick} {self._fmt_value(recv, unit, binary)}"
                all_ifaces.append(f"[ {if_icon} {fmt_stacked(fmt_send, fmt_recv)} ]")
        self._rendered["full_text"] = " ".join(all_ifaces)

    async def loop(self, notify_update: Callable[[], Any]):
        while True:
            self.update()
            # No notify_update needed -- will be rendered with the next clock
            await asyncio.sleep(5)


class DebugBlock(BlockBase, BlockControllerBase):
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

    async def loop(self, notify_update: Callable[[], Any]):
        ...


class InputController(BlockControllerBase):
    def __init__(self, blocks: list[BlockBase], debug_block: DebugBlock):
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
            notify()
        except Exception as e:
            self.debug_block.record_error(e)


    async def loop(self, notify_update: Callable[[], Any]):
        reader = await create_file_reader(sys.stdin)
        json_stream_input = JSONStreamInput(reader, lambda x: self.update(x, notify_update), array_format=True)
        await json_stream_input.read_forever()


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
    network_block = NetworkBlock(nm)
    debug_block = DebugBlock()

    blocks = [
        network_block,
        audio_block,
        clock_block,
        debug_block,
    ]

    input_controller = InputController(blocks, debug_block)

    controllers = [
        clock_block,
        audio_block,
        network_block,
        input_controller,
    ]

    updaters = [
        asyncio.create_task(controller.loop(notify))
        for controller in controllers
    ]

    try:
        print('{"version":1,"click_events":true}\n[')
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

