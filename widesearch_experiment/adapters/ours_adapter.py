from .searchos_adapter import run as run_searchos


async def run(task, cfg, services, workdir, *, adaptive=True):
    return await run_searchos(task, cfg, services, workdir, ours=True, adaptive=adaptive)
