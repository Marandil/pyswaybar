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
import time
import subprocess
import sys
import psutil

from dataclasses import dataclass
from collections import defaultdict
from typing import Any, Awaitable, Callable, Self

## Configuration
# By default we use the preconfigured sway font, but for monospace needs (like stacked strings) we
# need a configured monospace font.
font_monospace = 'RobotoMonoNerdFont'


## Constant definitions
nf_md_volume_off = '\U000f0581'
nf_md_volume_low = '\U000f057f'
nf_md_volume_medium = '\U000f0580'
nf_md_volume_high = '\U000f057e'

nf_md_arrow_up_thick = '\U000f005e'
nf_md_arrow_down_thick = '\U000f0046'

nf_fa_globe = '\uf0ac'
nf_fa_desktop = '\uf108'
nf_fa_network_wired = '\uef09'
nf_fa_wifi = '\uf1eb'


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
                while self.array_format and self.buffer.startswith(','):
                    self.buffer = self.buffer[1:].lstrip()
                payload, end = self.json_decoder.raw_decode(self.buffer)
                self.decode_grace = self.default_grace
                self.buffer = self.buffer[end:].lstrip()
                while self.array_format and self.buffer.startswith(','):
                    self.buffer = self.buffer[1:].lstrip()
                self.callback(payload)
        except json.JSONDecodeError:
            # eprint(f"{self.__class__} {e=} {self.buffer=} {self.decode_grace=}")
            self.decode_grace -= 1
            self.buffer = self.buffer.lstrip()  # lstrip should always be safe
            if not self.decode_grace:
                raise

    async def read_forever(self):
        if self.array_format:
            curr_buffer = await self.reader.read(1)
            assert curr_buffer == b'['
        while not self.reader.at_eof():
            curr_buffer = await self.reader.read(self.buffer_limit)
            self.buffer += curr_buffer.decode()
            self.process_buffer()


## Monitors
class PipeWireMonitor:
    def __init__(self):
        self.task = asyncio.create_task(self.read_forever())
        self.nodes = {}
        self.nodes_by_name = {}
        self.devices = {}
        self.metadata: dict[str, dict[str, Any]] = defaultdict(dict)
        # ---
        self.volume = 0
        self.is_muted = False
        self.event = asyncio.Event()

    def notify_change(self):
        self.event.set()

    def try_delete_node(self, id: int):
        node = self.nodes.pop(id, None)
        if node is not None:
            del self.nodes_by_name[node['props']['node.name']]
            return True
        else:
            return False

    def try_delete_device(self, id: int):
        device = self.devices.pop(id, None)
        return device is not None

    def process_update(self, update: list[dict[str, Any]]):
        check_volume_update = False
        for entry in update:
            match entry:
                case {'id': id, 'info': None}:
                    self.try_delete_node(id) or self.try_delete_device(id)

                case {'id': id, 'type': "PipeWire:Interface:Metadata", 'props': props, 'metadata': metadata}:
                    self.metadata[props['metadata.name']].update(
                        {m['key']: m['value'] for m in metadata}
                    )
                    check_volume_update = True

                case {'id': id, 'type': "PipeWire:Interface:Node", 'info': info}:
                    node_name = info['props']['node.name']
                    self.nodes[id] = info
                    self.nodes_by_name[node_name] = info
                    check_volume_update = True

                case {'id': id, 'type': "PipeWire:Interface:Device", 'info': info}:
                    self.devices[id] = info
                    check_volume_update = True

                case _:
                    pass

        if check_volume_update:
            try:
                default_audio_sink = self.metadata['default']['default.audio.sink']['name']
                node = self.nodes_by_name[default_audio_sink]
                props = [p for p in node['params']['Props'] if 'volume' in p]
                if not props:
                    eprint(f"missing volume props for node {default_audio_sink}")
                volume = props[0]['volume']
                channelVolumes = props[0]['channelVolumes']
                volume *= sum(channelVolumes) / len(channelVolumes)
                volume = round(pow(volume, 1/3), 2)
                is_muted = props[0]['mute']
                if self.volume != volume or self.is_muted != is_muted:
                    self.volume = volume
                    self.is_muted = is_muted
                    self.notify_change()

            except KeyError:
                pass

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
    padding = "" if t2l > t1l else '\u00a0'*(t1l-t2l)
    return f'<span font="{font_monospace}"><span rise="{rise*3//2}" font_size="{fs}">{text1}</span><span letter_spacing="-{sp}" font_size="{fs}">{"\u00a0"*t1l}</span><span rise="-{rise//2}" font_size="{fs}">{text2}{padding}</span></span>'


## WIP: Blocks
class BlockBase(abc.ABC):
    @property
    @abc.abstractmethod
    def rendered(self) -> dict[str, str]:
        ...

    @abc.abstractmethod
    def onclick(self, rel_x: float, rel_y: float):
        ...


class ClockBlock(BlockBase):
    def __init__(self):
        self._rendered = {"name": "clock", "full_text": "(startup)"}

    @property
    def rendered(self):
        return self._rendered

    def onclick(self, rel_x: float, rel_y: float):
        ...

    def update(self):
        fmt_now = time.strftime("%Y-%m-%d %H:%M:%S")
        self._rendered['full_text'] = fmt_now

    async def loop(self, notify_update: Callable[[], Awaitable[Any]]):
        N = 599
        while True:
            # First time each N seconds sleep less than 1 second to align with the wall clock
            self.update()
            await notify_update()
            adj = time.clock_gettime(time.CLOCK_REALTIME) % 1
            await asyncio.sleep(1 - adj)
            for _ in range(N):
                self.update()
                await notify_update()
                await asyncio.sleep(1)


class AudioBlock(BlockBase):
    def __init__(self, pwm: PipeWireMonitor):
        self._rendered = {"name": "audio", "full_text": "(startup)"}
        self.pwm = pwm

    @property
    def rendered(self) -> dict[str, str]:
        return self._rendered

    def onclick(self, rel_x: float, rel_y: float):
        if rel_x < 0.5:
            act_toggle_mute()

    def update(self):
        volume, is_muted = self.pwm.volume, self.pwm.is_muted
        if is_muted:
            icon = nf_md_volume_off
        elif volume < 0.3:
            icon = nf_md_volume_low
        elif volume < 0.6:
            icon = nf_md_volume_medium
        else:
            icon = nf_md_volume_high

        self._rendered["full_text"] = f"{icon} {volume:.0%}"

    async def loop(self, notify_update: Callable[[], Awaitable[Any]]):
        pwm_event = self.pwm.event
        while True:
            self.update()
            await notify_update()
            await pwm_event.wait()
            pwm_event.clear()


class NetworkMetric(enum.IntEnum):
    Bytes = 0
    ByteRate = 1
    Packets = 2
    PacketRate = 3

    def next(self) -> Self:
        next = (self.value + 1) % len(self.__class__)
        return self.__class__(next)


class NetworkBlock(BlockBase):
    def __init__(self, nm):
        self._rendered = {"name": "network", "full_text": "(startup)", "markup": "pango"}
        self.nm = nm
        self.metric = NetworkMetric.Bytes

    @property
    def rendered(self) -> dict[str, str]:
        return self._rendered

    def onclick(self, rel_x: float, rel_y: float):
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

    async def loop(self, notify_update: Callable[[], Awaitable[Any]]):
        while True:
            self.update()
            # No notify_update needed -- will be rendered with the next clock
            await asyncio.sleep(5)


## Action definitions
def act_toggle_mute():
    asyncio.create_task(sub_call("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"))


## Main loop(s)
async def main():
    pwm = PipeWireMonitor()
    nm = NetworkMonitor()

    fld_event = ""
    update_queue = asyncio.Queue()

    # -- block approach --
    clock_block = ClockBlock()
    audio_block = AudioBlock(pwm)
    network_block = NetworkBlock(nm)

    async def process_stdin():
        def update(event):
            nonlocal fld_event
            # fld_event = f'{event=}'
            name, rel_x, rel_y, width, height = \
                event['name'], event['relative_x'], event['relative_y'], event['width'], event['height']
            x_pct = rel_x / width
            y_pct = rel_y / height
            fld_event = f'{name} @ {x_pct:3.1%}, {y_pct:3.1%}'
            match name:
                case 'network':
                    network_block.onclick(x_pct, y_pct)
                case 'audio':
                    audio_block.onclick(x_pct, y_pct)
                case 'clock':
                    clock_block.onclick(x_pct, y_pct)
                case _:
                    fld_event += " [unhandled]"
            asyncio.create_task(update_queue.put(None))

        reader = await create_file_reader(sys.stdin)
        json_stream_input = JSONStreamInput(reader, update, array_format=True)
        await json_stream_input.read_forever()

    async def notify():
        await update_queue.put(None)

    updaters = [
        asyncio.create_task(clock_block.loop(notify)),
        asyncio.create_task(audio_block.loop(notify)),
        asyncio.create_task(network_block.loop(notify)),
        # ---
        asyncio.create_task(process_stdin())
    ]

    try:
        print('{"version":1,"click_events":true}\n[')
        while True:
            try:
                msg = [
                    # Uncomment event to debug mouse events
                    # {"full_text":fld_event, "name":"event"},
                    network_block.rendered,
                    audio_block.rendered,
                    clock_block.rendered,
                ]
                msg = json.dumps(msg)
                print(f'{msg},')
                await update_queue.get()
            except Exception as e:
                print(f"Error: {e!r}")
    finally:
        for task in updaters:
            task.cancel()
        await pwm.close()

asyncio.run(main())

