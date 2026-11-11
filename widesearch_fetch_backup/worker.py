"""Install common readers before adapters import Services in an isolated worker."""
from widesearch_experiment import services
from .services import BackupServices


def main():
    services.Services = BackupServices
    from widesearch_baselines.worker import main as baseline_worker
    baseline_worker()


if __name__ == "__main__":
    main()
