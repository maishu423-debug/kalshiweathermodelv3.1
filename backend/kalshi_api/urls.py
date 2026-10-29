from django.urls import path

from . import views


urlpatterns = [
    path("health/", views.health),
    path("weather/source",             views.weather_source_health),
    path("weather/source/",            views.weather_source_health),
    path("miami-temperature",         views.miami_temperature),
    path("miami-temperature/",        views.miami_temperature),
    path("miami-temperature/stream",  views.miami_temperature_stream),
    path("miami-temperature/stream/", views.miami_temperature_stream),
    path("paper/state",               views.paper_state),
    path("paper/state/",              views.paper_state),
    path("paper/order",               views.paper_order),
    path("paper/order/",              views.paper_order),
    path("paper/reset",               views.paper_reset),
    path("paper/reset/",              views.paper_reset),
    # Algorithm
    path("algorithm/state",           views.algorithm_state),
    path("algorithm/state/",          views.algorithm_state),
    path("autotrader/state",          views.autotrader_state),
    path("autotrader/state/",         views.autotrader_state),
    path("autotrader/toggle",         views.autotrader_toggle),
    path("autotrader/toggle/",        views.autotrader_toggle),
    path("autotrader/notify-test",    views.notify_test),
    path("autotrader/notify-test/",   views.notify_test),
    path("autotrader/reset",          views.autotrader_reset),
    path("autotrader/reset/",         views.autotrader_reset),
    path("algorithm/refresh",         views.algorithm_refresh),
    path("algorithm/refresh/",        views.algorithm_refresh),
    path("algorithm/cron-refresh",    views.algorithm_cron_refresh),
    path("algorithm/cron-refresh/",   views.algorithm_cron_refresh),
    path("algorithm/refresh-temp",    views.algorithm_refresh_temp),
    path("algorithm/refresh-temp/",   views.algorithm_refresh_temp),
    path("algorithm/forecast-history",  views.forecast_history),
    path("algorithm/forecast-history/", views.forecast_history),
    path("algorithm/trade-history",     views.trade_history),
    path("algorithm/trade-history/",    views.trade_history),
]
