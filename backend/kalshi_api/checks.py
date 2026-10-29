"""
Django system checks for the live weather source.

`python manage.py migrate` (the Railway start command) runs these, so a bad ASOS_SOURCE / LDM_API_* setting
stops the deploy with a clear message instead of silently running without data.
"""
from django.core import checks


@checks.register()
def weather_source_configuration(app_configs, **kwargs):
    from weather.ldm_client import validate_source_config

    return [
        checks.Error(
            problem,
            hint="Fix the backend service variables, or set ASOS_SOURCE=public to use NOAA's public files.",
            id="weather.E001",
        )
        for problem in validate_source_config()
    ]
