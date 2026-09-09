from XBotv2.jobs.contracts import JobsPort
from XBotv2.jobs.registry import JobRegistry


def test_job_registry_implements_jobs_port():
    required = {
        name
        for name in JobsPort.__dict__
        if not name.startswith("_")
    }
    assert required <= set(dir(JobRegistry))