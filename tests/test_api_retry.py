"""fetch_page: 429 / 5xx / connection retry policy and non-retryable failures (plan sections 5 and 14)."""
import copy

import pytest
import requests

from helpers import FakeHttpGet, FakeResponse, LE, base_cfg_dict, make_page, write_cfg
from src import api
from src.api import ApiHttpError, RateLimitExhausted, RetryExhausted

PAYLOAD = make_page("BRA", LE, [2024, 2023, 2022], 1, 1, 3, 3)
KEY = ("BRA", LE, 1)
OK = FakeResponse(200, PAYLOAD)


def run(cfg, seq, fake_sleep):
    fake = FakeHttpGet(responses={KEY: tuple(seq)}, auto=False)
    url = api.build_url(cfg, "BRA", LE, 1)
    return fake, url, lambda: api.fetch_page(url, cfg, http_get=fake, sleep=fake_sleep)


def r(status):
    return FakeResponse(status, None, text="err")


def test_success_first_try(cfg, fake_sleep):
    fake, url, call = run(cfg, [OK], fake_sleep)
    assert call() == PAYLOAD
    assert fake.calls == [url]
    assert fake_sleep.calls == []


def test_http_get_receives_timeout_from_config(cfg, fake_sleep):
    fake, url, call = run(cfg, [OK], fake_sleep)
    call()
    assert fake.timeouts == [17]


def test_429_then_200(cfg, fake_sleep):
    fake, url, call = run(cfg, [r(429), OK], fake_sleep)
    assert call() == PAYLOAD
    assert len(fake.calls) == 2 and fake.calls[0] == fake.calls[1] == url
    assert fake_sleep.calls == [60]


def test_three_429_then_200_succeeds(cfg, fake_sleep):
    fake, url, call = run(cfg, [r(429)] * 3 + [OK], fake_sleep)
    assert call() == PAYLOAD
    assert len(fake.calls) == 4
    assert fake_sleep.calls == [60, 60, 60]


def test_four_429_raises_rate_limit_exhausted(cfg, fake_sleep):
    fake, url, call = run(cfg, [r(429)] * 4, fake_sleep)
    with pytest.raises(RateLimitExhausted) as ei:
        call()
    assert len(fake.calls) == 4
    assert fake_sleep.calls == [60, 60, 60]
    assert isinstance(ei.value, RetryExhausted)
    assert ei.value.url == url
    assert ei.value.attempts == 4
    assert url in str(ei.value) and "429" in str(ei.value)


@pytest.mark.parametrize("status", [500, 502, 503, 599])
def test_5xx_then_200(cfg, fake_sleep, status):
    fake, url, call = run(cfg, [r(status), OK], fake_sleep)
    assert call() == PAYLOAD
    assert fake_sleep.calls == [60]
    assert len(fake.calls) == 2


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_exhaustion_raises_retry_exhausted_not_rate_limit(cfg, fake_sleep, status):
    fake, url, call = run(cfg, [r(status)] * 4, fake_sleep)
    with pytest.raises(RetryExhausted) as ei:
        call()
    assert not isinstance(ei.value, RateLimitExhausted)
    assert len(fake.calls) == 4
    assert fake_sleep.calls == [60, 60, 60]
    assert ei.value.attempts == 4 and ei.value.url == url
    assert str(status) in str(ei.value)


@pytest.mark.parametrize("exc", [requests.ConnectionError("boom"), requests.Timeout("slow")])
def test_connection_errors_use_same_policy(cfg, fake_sleep, exc):
    fake, url, call = run(cfg, [exc, OK], fake_sleep)
    assert call() == PAYLOAD
    assert fake_sleep.calls == [60]
    second_sleep = type(fake_sleep)()
    fake, url, call = run(cfg, [exc] * 4, second_sleep)
    with pytest.raises(RetryExhausted) as ei:
        call()
    assert not isinstance(ei.value, RateLimitExhausted)
    assert len(fake.calls) == 4
    assert second_sleep.calls == [60, 60, 60]


def test_mixed_causes_share_one_counter(cfg, fake_sleep):
    seq = [r(429), r(503), requests.ConnectionError("x"), r(429)]
    fake, url, call = run(cfg, seq, fake_sleep)
    with pytest.raises(RateLimitExhausted):  # last cause was a 429
        call()
    assert len(fake.calls) == 4 and fake_sleep.calls == [60, 60, 60]


def test_mixed_causes_last_cause_not_429(cfg, fake_sleep):
    seq = [r(429), r(429), r(429), r(503)]
    fake, url, call = run(cfg, seq, fake_sleep)
    with pytest.raises(RetryExhausted) as ei:
        call()
    assert not isinstance(ei.value, RateLimitExhausted)


def test_mixed_causes_succeed_within_budget(cfg, fake_sleep):
    seq = [r(503), requests.Timeout("t"), r(429), OK]
    fake, url, call = run(cfg, seq, fake_sleep)
    assert call() == PAYLOAD
    assert fake_sleep.calls == [60, 60, 60]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 418])
def test_non_retryable_4xx_raises_api_http_error_immediately(cfg, fake_sleep, status):
    fake, url, call = run(cfg, [r(status), OK], fake_sleep)
    with pytest.raises(ApiHttpError):
        call()
    assert len(fake.calls) == 1
    assert fake_sleep.calls == []


def test_non_json_200_raises_api_http_error_immediately(cfg, fake_sleep):
    fake, url, call = run(cfg, [FakeResponse(200, FakeResponse.NOT_JSON, text="<html>oops</html>"), OK],
                          fake_sleep)
    with pytest.raises(ApiHttpError):
        call()
    assert len(fake.calls) == 1 and fake_sleep.calls == []


def test_other_request_exception_wrapped_and_chained(cfg, fake_sleep):
    original = requests.exceptions.InvalidURL("bad url")
    fake, url, call = run(cfg, [original, OK], fake_sleep)
    with pytest.raises(ApiHttpError) as ei:
        call()
    assert ei.value.__cause__ is original
    assert len(fake.calls) == 1 and fake_sleep.calls == []


def test_exceptions_are_pipeline_errors():
    names = lambda cls: [c.__name__ for c in cls.__mro__]
    for cls in (RetryExhausted, RateLimitExhausted, ApiHttpError):
        assert "PipelineError" in names(cls)
    assert issubclass(RateLimitExhausted, RetryExhausted)
    assert not issubclass(ApiHttpError, RetryExhausted)


# ----------------------------------------------------------------------------- config drives the policy
def cfg_with(load_cfg, **retry):
    d = base_cfg_dict()
    d["retry"].update(retry)
    return load_cfg(d)


def test_sleep_seconds_and_max_retries_come_from_config(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, sleep_seconds=5, max_retries=1)
    fake, url, call = run(cfg, [r(429), r(429), OK], fake_sleep)
    with pytest.raises(RateLimitExhausted):
        call()
    assert len(fake.calls) == 2 and fake_sleep.calls == [5]


def test_max_retries_larger_than_default(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, sleep_seconds=2, max_retries=5)
    fake, url, call = run(cfg, [r(429)] * 5 + [OK], fake_sleep)
    assert call() == PAYLOAD
    assert fake_sleep.calls == [2] * 5


def test_max_retries_zero_means_no_retry(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, max_retries=0)
    fake, url, call = run(cfg, [r(429), OK], fake_sleep)
    with pytest.raises(RateLimitExhausted):
        call()
    assert len(fake.calls) == 1 and fake_sleep.calls == []


def test_retry_on_5xx_false_raises_api_http_error_without_sleep(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, retry_on_5xx=False)
    fake, url, call = run(cfg, [r(503), OK], fake_sleep)
    with pytest.raises(ApiHttpError):
        call()
    assert len(fake.calls) == 1 and fake_sleep.calls == []


def test_retry_on_connection_errors_false_is_not_retried(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, retry_on_connection_errors=False)
    fake, url, call = run(cfg, [requests.ConnectionError("x"), OK], fake_sleep)
    with pytest.raises(ApiHttpError):
        call()
    assert len(fake.calls) == 1 and fake_sleep.calls == []


def test_status_codes_list_controls_retryable_statuses(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, status_codes=[429, 408])
    fake, url, call = run(cfg, [r(408), OK], fake_sleep)
    assert call() == PAYLOAD and fake_sleep.calls == [60]
    cfg = cfg_with(load_cfg, status_codes=[429])
    fake, url, call = run(cfg, [r(408), OK], type(fake_sleep)())
    with pytest.raises(ApiHttpError):
        call()


def test_5xx_not_retried_via_status_codes_when_flag_false_but_listed(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, retry_on_5xx=False, status_codes=[429, 503])
    fake, url, call = run(cfg, [r(503), OK], fake_sleep)
    assert call() == PAYLOAD and fake_sleep.calls == [60]


def test_429_retried_even_if_5xx_flag_false(load_cfg, fake_sleep):
    cfg = cfg_with(load_cfg, retry_on_5xx=False)
    fake, url, call = run(cfg, [r(429), OK], fake_sleep)
    assert call() == PAYLOAD and fake_sleep.calls == [60]


def test_http_get_and_sleep_are_injected_not_global(cfg, fake_sleep):
    """The network guard replaces requests.get with a raiser; fetch_page must use the injected callable."""
    fake, url, call = run(cfg, [OK], fake_sleep)
    assert call() == PAYLOAD
