# pyswaybar

A quick-and-dirty Swaybar server in Python.
Assumes PipeWire audio.

## Philosophy

Typical bare Swaybar setup involves calling a shell script periodically like this:

```bash
while my-swaybar-script; do sleep 1; done
```

With the script creating multiple subprocesses each second to poll for each
displayed piece of information:

```bash
#!/bin/bash

time_and_date=$(date "+%Y-%m-%d %H:%M")

audio_volume=$(wpctl get-volume @DEFAULT_AUDIO_SINK@)

# ...

echo "$network_status | $battery_status | $audio_volume | $time_and_date"
```

This not only spams the system with completely unnecessary processes (assuming
spawning at least 5 subprocesses each second), but also updates at a rate of a
signle hertz, even if the data is no longer valid or would be more helpful at
a higher rate. My personal pet peeve is updating volume, because I want to see
the change immediately, when using keyboard shortcuts to update it.

Let us also remember, that even if the rate is low, the requirement of spawning
5 processes each second does not come without a cost. The cost is mostly hidden
from plain sight, because the individual processes are short-lived and do not
report accumulated user/system time in tools like {h,b,}top.

Our philosophy is to provide a configurable and extensible sway-bar script that
avoids spawning subprocesses and allows for a low latency updates when needed.
Our first integration was with PipeWire `pw-dump` in monitor mode to observe
changes in the audio subsystem in near-real-time.

We utilize Python's asyncio framework to provide a lightweight, single-thread
concurrency.

## Setup

```bash
cp ./main.py ~/.config/sway/sway-bar-status

$EDITOR ~/.config/sway/config
```

```conf
# Read `man 5 sway-bar`
bar {
    position top

    status_command ~/.config/sway/sway-bar-status

    colors {
        // Up to you
    }
}
```

## Configuration

For now just edit the source. No explicit configuration options available.

