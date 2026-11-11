"""Same CLI as the baseline package, with a separately frozen reader version."""


def main():
    from widesearch_baselines import __main__ as cli
    from .launcher import DEFAULT_CONFIG, run_batch
    cli.DEFAULT_CONFIG = DEFAULT_CONFIG
    cli.run_batch = run_batch
    cli.main()


if __name__ == "__main__":
    main()
