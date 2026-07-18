#!/usr/bin/env python3
import asyncio
import logging
import sys
from pathlib import Path

from aiohttp import web

from carcharge.config import load_config
from carcharge.epspot import EpspotClient
from carcharge.mercedes import MercedesClient
from carcharge.scheduler import ChargingService
from carcharge.state import state
from carcharge.trips import load_trips
from carcharge.web import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

CONFIG_PATH = Path("/data/config.yaml")
WEB_PORT = 8080


async def main() -> None:
    log.info("carcharge starting")
    cfg = load_config(str(CONFIG_PATH))

    async with EpspotClient(cfg.epspot.email, cfg.epspot.password) as epspot:
        await epspot.login()
        state.epspot_ok = True
        state.epspot_user = epspot._user_name or cfg.epspot.email
        state.basic_soc_pct = cfg.charging.basic_soc_pct
        state.trip_soc_pct = cfg.charging.trip_soc_pct
        state.target_soc_pct = cfg.charging.basic_soc_pct  # effective target starts at basic
        state.charge_rate_kw = cfg.vehicle.charge_rate_kw
        state.battery_capacity_kwh = cfg.vehicle.battery_capacity_kwh
        state.charging_efficiency = cfg.vehicle.charging_efficiency
        load_trips()

        async with MercedesClient(
            email=cfg.mercedes.email,
            password=cfg.mercedes.password,
            vin=cfg.mercedes.vin,
            region=cfg.mercedes.region,
        ) as mercedes:
            service = ChargingService(cfg, epspot, mercedes)

            # Web UI
            webapp = create_app()
            runner = web.AppRunner(webapp)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
            await site.start()
            log.info("Web UI at http://0.0.0.0:%d", WEB_PORT)

            await service.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
