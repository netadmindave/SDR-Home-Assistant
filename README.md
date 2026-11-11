# SDR Home Assistant add-ons

[![Add repository to my Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnetadmindave%2FSDR-Home-Assistant)

## RTL-SDR Survey

![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg) ![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)

One RTL-SDR dongle, time-sliced between three tools:

- **rtl_433** decodes 300–928 MHz sensors: weather, temp/humidity, TPMS, door/window, remotes.
- **rtl_power** runs peak-hold sweeps of the unlicensed bands to find transmitters nothing decoded.
- **radiosonde_auto_rx** tracks NWS weather balloons during the twice-daily launch windows.

**Survey mode** runs for days and builds an inventory of everything it hears. **Approve** the devices you want, and they appear in Home Assistant through MQTT discovery. **Monitor mode** then parks the dongle on the frequencies your approved devices use.

Full documentation: [rtlsdr_survey/DOCS.md](rtlsdr_survey/DOCS.md)

### Install

1. Click the button above, or go to Settings → Add-ons → Add-on Store → ⋮ → Repositories and add
   `https://github.com/netadmindave/SDR-Home-Assistant`.
2. Install **RTL-SDR Survey**, start it, and open **SDR Survey** in the sidebar.

To run it without Home Assistant OS, see the `docker-compose.yml` in this repo.

## Credits

This add-on wraps these projects; all credit for the decoding goes to them:
[rtl_433](https://github.com/merbanan/rtl_433) (GPL-2.0),
[radiosonde_auto_rx](https://github.com/projecthorus/radiosonde_auto_rx) (GPL-3.0),
[rtl-sdr-blog librtlsdr](https://github.com/rtlsdrblog/rtl-sdr-blog) (GPL-2.0).
