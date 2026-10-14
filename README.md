# hass-smartbox
![hassfest](https://github.com/ajtudela/hass-smartbox/workflows/Validate%20with%20hassfest/badge.svg) [![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration) [![codecov](https://codecov.io/gh/ajtudela/hass-smartbox/branch/main/graph/badge.svg?token=C6J448TUQ8)](https://codecov.io/gh/ajtudela/hass-smartbox) [![Total downloads](https://img.shields.io/github/downloads/ajtudela/hass-smartbox/total)](https://github.com/ajtudela/hass-smartbox/releases) [![Downloads of latest version (latest by SemVer)](https://img.shields.io/github/downloads/ajtudela/hass-smartbox/latest/total?sort=semver)](https://github.com/ajtudela/hass-smartbox/releases/latest) [![Current version](https://img.shields.io/github/v/release/ajtudela/hass-smartbox)](https://github.com/ajtudela/hass-smartbox/releases/latest)


Home Assistant integration for Haverland (and other brands) heating smartboxes.


## Installation

### Using HACS (Recommended)

1. Add this repository to your custom repositories
1. Search for and install "Smartbox" in HACS.
1. Restart Home Assistant.


### Manually Copy Files

1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
1. If you do not have a `custom_components` directory (folder) there, you need to create it.
1. In the `custom_components` directory (folder) create a new folder called `smartbox`.
1. Download _all_ the files from the `custom_components/smartbox/` directory (folder) in this repository.
1. Copy the `smartbox_protocol/` and `smartbox_core/` subdirectories recursively - they are vendored Python packages the integration imports.
1. Place the files you downloaded in the new directory (folder) you created.
1. Restart Home Assistant


### Finally

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=smartbox)


## Configuration

You will need the following items of information:
* Name of your reseller
* Your username and password used for the mobile app/web app.

If there is an issue during the process or authentication, the errors will be displayed.

### Additional Options
You can also specify the following options (although they have reasonable defaults)

#### Consumption history options
We are currently getting the [consumption](#consumption) of device through the API and we inject it in statistics and TotalConsumption sensor
* `start` : we will get the last 3 years of consumption and set the option to auto.
* `auto` : every 15 minutes, we get the last 24 hours.
* `off` : stop the automatic collect. We will still update the sensor's value every 15 minutes.

#### Reseller logo
By default, each sensor has in own icon depends on the type of the sensor.
You can activate this option to display the logo of the reseller instead.

#### Timedelta between update power
If you have a [Dedicated energy monitor](#dedicated-energy-monitor), we get the current power each 60 seconds by default.
You can update this time with this option.
> [!NOTE]
> Be carefull with this option, reduce the number little by little to see if any instability occurs.

### Architecture

Since 3.0.0 the integration runs on a purpose-built three-layer stack
(the `smartbox` library is no longer a runtime dependency: its
reseller table, node-type enum and backoff helper are absorbed into
the integration):

* `smartbox_protocol` — auth (token manager), the REST client, the
  `ws_user` websocket transport and the named write operations.
* `smartbox_core` — the versioned state store + engine: snapshot
  ingestion, websocket-frame merging, availability tracking and
  write-confirmation windows.
* `custom_components/smartbox` — the HA adapter: config flow, entity
  platforms, diagnostics.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the layer rules, the repository
map, and the read and write flows.

### Websocket transport

Status updates arrive over the `ws_user` channel (the vendor web apps'
transport): ONE connection for the whole account. Its server-side
proxy PINGs keep the session supervised — the periodic "Server has
stopped communicating" aborts of the legacy per-device `socket_io`
transport disappear. The server cuts the socket at the access token's
4-hour expiry (~4-second outage while it reconnects with a fresh
token); that is the steady state, by design.

> [!WARNING]
> **BREAKING:** the socket_io fallback is GONE. The setup probes the
> account's API host once with a fresh token; a host that does not
> serve `ws_user` cannot work with this version (the config entry
> fails with an "unsupported" error). `ws_user` is live-verified on
> the api-lhz host; other hosts are unverified, not known-bad — check
> your API host via the vendor app's Version menu. Resellers/hosts
> that only serve socket_io must stay on 2.6.x. Transient probe
> trouble (a 5xx-ing or unreachable host) retries via the normal
> `ConfigEntryNotReady`
> retry — a transport verdict is never inferred from transient
> trouble.

The transport is visible in Settings → Devices → diagnostics as
`runtime.transport`.

### Write confirmation

Writes to a node arm a confirmation window: the write is applied
optimistically in Home Assistant, and a confirming frame from the node
within the window confirms it. If no confirmation arrives (e.g. the
heater is unreachable), the node's entities turn Unavailable — the
same behavior the vendor app shows when a command does not land.

## Features

### Heaters (climate)
Heater node types (`htr`, `acm` and `htr_mod`) are modelled as Home
Assistant Climate entities.

* `htr` and `acm` (accumulator) nodes
  * Supported modes: 'manual' and 'auto'
  * Supported presets: 'home' and 'away'
* `htr_mod`
  * Supported modes: 'manual', 'auto', 'self_learn' and 'presence'
  * Supported presets: 'comfort', 'eco', 'frost', 'schedule', 'self_learn' and 'activity'

Every heater also exposes the 'away' and 'boost' presets (boost where
the device supports it) and the `OFF` HVAC mode. The modes and presets
for `htr_mod` heaters are mapped as follows:

| htr\_mod mode | htr\_mod selected_temp | HA HVAC mode | HA preset   |
|---------------|------------------------|--------------|-------------|
| manual        | comfort                | HEAT         | COMFORT     |
|               | eco                    | HEAT         | ECO         |
|               | ice                    | HEAT         | FROST       |
| auto          | *                      | AUTO         | SCHEDULE    |
| self\_learn   | *                      | AUTO         | SELF\_LEARN |
| presence      | *                      | AUTO         | ACTIVITY    |

The target temperature step is 0.5 °C / 1.0 °F. On the fw-1.9 `htr`
family, the maximum target temperature is clamped to the node's
`max_stemp_limit` when the device advertises one.

### Node availability
Heaters the box reports as unreachable (node-level `sync_status:
"lost"`) show as Unavailable on all of their entities, so a heater whose
power is cut is visible at a glance. Entities come back as Available
shortly after the node answers again.

### Sensors
* Temperature (current)
* Power (including PMO dedicated energy monitors)
* Duty cycle (`htr` nodes)
* Total consumption (energy, with recorder statistics import - see
  [Consumption](#consumption))
* Charge level (`acm` nodes)
* Boost end time (timestamp)
* Schedule (enum, per heater node)
* RTC clock drift (box-level, refreshed every 5 minutes)

### Binary sensor
One box-level connectivity sensor (per-node connectivity is shown
through the climate entities' availability instead).

### Switches
* Box level: away mode, "no power limit"
* Per node: window mode, true radiant, boost, child lock (keypad
  lockout), maximum temperature limit (remembers the last non-zero
  limit across Home Assistant restarts)

### Numbers
* Box power limit (0 = no limit, max 60000)
* Per node: boost temperature, boost duration, away offset, maximum
  temperature, and programme temperatures (frost / eco / comfort, where
  the device reports them)

### Selects
* Per node: radiator priority (low / medium / high, fw-1.9 family
  only) — the device takes it into account itself when enforcing the
  box power limit

### Dedicated energy monitor
The PMO devices are available including the power limit entity.

### Consumption
The smartbox API is only giving the hourly consumption from a start and an end period of time.
You can't have real time consumption of a device and this consumption is always increasing.

At every beginning of an hour, during around 15/20 minutes, the API do not provide data for the current hour.
So we always ask for the last three hours and report the most recent counter in that window, so a missing hour cannot make the consumption drop.

Every 15 minutes, we are updating data sensor with the most recent data. You are able to see the consumption directly into the history graph of the sensor.

But to be sure we ensure the right data to the right hour, we also get the last 24 hours and upsert these data into statistics to avoid time difference and some data drop.
> [!TIP]
> If you don't want to upsert these 24 hours, you have to set the [option](#consumption-history-options) to `off`.

#### History
The first time we create a config entry (or when the [option](#consumption-history-options) of the config entry is set to `start`) we get the last 3 years of consumption.
As it is not possible to add it directly to the sensor data, we insert it into the statistics of the sensor.
So it let the energy dashboard working with the current and back history.


> [!TIP]
> If you want to reset all the data, you have to set the [option](#consumption-history-options) to `start`.

## FAQ
#### There is negative consumption in the energy dashboard
There might be a huge negative consumption in your energy dashboard. The consumption [history](#history) should deal with it. But sometimes it didn't work.
You have two options:
* Settings the [option](#consumption-history-options) to `start` : it will force load all data.
* Go to [![Open your Home Assistant instance and show your statistics developer tools.](https://my.home-assistant.io/badges/developer_statistics.svg)](https://my.home-assistant.io/redirect/developer_statistics/), select the total consumption entity, outliers and patch the negative value with 0.

#### My Reseller is not present in the list
If you can't see your reseller which is using a smartbox you have to do an [Reseller Github issue].

## Debugging

### Diagnostics

If there is an error and raise an issue in [Github issue], please attach the diagnostics of the entry :
* Go on [![Open your Home Assistant instance and show an integration.](https://my.home-assistant.io/badges/integration.svg)](https://my.home-assistant.io/redirect/integration/?domain=smartbox)
* Click on the three dots of the config entry
* Download diagnostics

### System health

You can see if all the smartbox component are available here [![Open your Home Assistant instance and show information about your system.](https://my.home-assistant.io/badges/system_health.svg)](https://my.home-assistant.io/redirect/system_health/)

### Logs
Debug logging can be enabled by increasing the log level for the
integration and its vendored protocol/core layers in the Home
Assistant `configuration.yaml`:

```
 logger:
   ...
   logs:
     custom_components.smartbox: debug
     custom_components.smartbox.smartbox_protocol: debug
     custom_components.smartbox.smartbox_core: debug
   ...
```

See the [Home Assistant logger docs] for how to view the actual logs. Please
file a [Github issue] with any problems.

## Development / live probing

For wire investigation against the real hosts (without a live HA
deploy), `tools/live_probe.py` drives the vendored protocol layer from
the CLI. It needs YOUR OWN `.env` file at the repo root (gitignored)
with:

```
SMARTBOX_API_NAME=...
SMARTBOX_USERNAME=...
SMARTBOX_PASSWORD=...
```

(The reseller table supplies the Basic-auth/serial-id/referer values,
so no extra key is needed.) Tokens, credentials and device IDs are
masked in the output; a token is never printed - only its length,
expiry and claim facts are.

Commands:

- `uv run python tools/live_probe.py auth` — password grant + one
  refresh grant (rotation check).
- `... probe` — ws_user capability probe verdict.
- `... reads [--dev DEV_ID] [--node TYPE:ADDR]` — grouped devices,
  then per node the raw status/setup/version JSON (and
  `--samples-from/--samples-to` for the samples window).
- `... ws [--duration 60] [--heartbeat 60]` — subscribe the ws_user
  transport; event counts, bounded update previews, transport facts.
- `... write --dev DEV_ID --node TYPE:ADDR
  --i-know-this-writes-to-my-heater <op> ...` — WriteApi ops
  (`set_mode`, `set_setpoint`, `engage_modified_auto`, `set_lock`,
  `set_boost`, `set_prog_temps`, `set_window_mode`, `set_true_radiant`,
  `set_away_offset`, `set_max_stemp_limit`, `set_priority`, `set_away`,
  `set_power_limit`, `set_prog`); prints the exact body before sending,
  then GETs the affected resource (the server's read-through may lag
  several seconds and show the old value).
- `... all` — probe + reads (the daily sanity menu).
> [!NOTE]
> The initial version of this integration was made by [graham33](https://github.com/graham33) but it was not maintained.

# Support
Ajtudela [![Buy a coffee to ajtudela][buymeacoffee-shield]][buymeacoffee-ajtudela]

Delmael [![Buy a coffee to delmael][buymeacoffee-shield]][buymeacoffee-delmael]

[buymeacoffee-ajtudela]: https://www.buymeacoffee.com/ajtudela
[buymeacoffee-delmael]: https://www.buymeacoffee.com/delmael

[buymeacoffee-shield]: https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png
[custom repository]: https://hacs.xyz/docs/faq/custom_repositories
[Github issue]: https://github.com/ajtudela/hass-smartbox/issues
[Reseller Github issue]: https://github.com/ajtudela/smartbox/issues/new?template=new-reseller.md
[Home Assistant integration docs]: https://developers.home-assistant.io/docs/creating_integration_file_structure/#where-home-assistant-looks-for-integrations
[Home Assistant logger docs]: https://www.home-assistant.io/integrations/logger/#viewing-logs
[Home Assistant secrets management]: https://www.home-assistant.io/docs/configuration/secrets/
