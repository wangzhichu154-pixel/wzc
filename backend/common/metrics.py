from prometheus_client import Counter, Histogram

REQUESTS = Counter("vector_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("vector_http_request_duration_seconds", "HTTP latency", ["method", "route"], buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 15))
