"""Unit tests for :mod:`sakarma.scraper.client`.

All HTTP traffic is mocked via :mod:`responses`. HTML stubs are pinned inline
so the parser exercises the verified hidden-input shape; richer HTML golden
fixtures arrive in Unit 6.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

import pytest
import requests
import responses

from sakarma.config import settings as real_settings
from sakarma.scraper.client import (
    PaginationDetectedError,
    SakarmaClient,
    SessionExpiredError,
)
from sakarma.scraper.protocol import (
    BTN_APPV_MEETINGS,
    DDL_DISTRICT,
    LBWISE_PATH,
    PUBLIC_MINUTES_PATH,
    FormState,
    decision_attachment_target,
    grid_dr_target,
    parse_form_state,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
class _StubLimiter:
    @contextmanager
    def acquire(self):
        yield


class _StubSettings:
    """Lightweight stand-in for the real Settings; avoids env-var coupling."""

    scraper_base_url = "https://meeting.lsgkerala.gov.in"
    scraper_delay_min = 0.0
    scraper_delay_max = 0.0
    scraper_request_timeout = 10
    scraper_max_retries = 3
    scraper_backoff_base = 0.01
    scraper_backoff_max = 0.05
    user_agent = "sakarma-test/1.0"


@pytest.fixture
def stub_settings() -> _StubSettings:
    return _StubSettings()


@pytest.fixture
def stub_limiter() -> _StubLimiter:
    return _StubLimiter()


@pytest.fixture
def client(stub_settings: _StubSettings, stub_limiter: _StubLimiter) -> SakarmaClient:
    session = requests.Session()
    return SakarmaClient(
        session=session, settings=stub_settings, rate_limiter=stub_limiter
    )


# ---------------------------------------------------------------------------
# HTML stub builders
# ---------------------------------------------------------------------------
def _dashboard_html(
    *,
    district_value: str = "1",
    extra_select: str = "",
    pager: bool = False,
) -> str:
    pager_html = (
        '<tr class="pager"><td>'
        '<a href="javascript:__doPostBack(\'GridMeetingDEtails\',\'Page$2\')">2</a>'
        "</td></tr>"
        if pager
        else ""
    )
    return f"""<!doctype html>
<html><body>
<form id="form1" method="post" action="LBWiseDashBoard.aspx">
  <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="VS_TOKEN" />
  <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="VSG_TOKEN" />
  <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="EV_TOKEN" />
  <input type="hidden" name="__LASTFOCUS" id="__LASTFOCUS" value="" />
  <select name="{DDL_DISTRICT}" id="ddlDistrict">
    <option value="0">--</option>
    <option value="1" selected>Thiruvananthapuram</option>
    <option value="2">Kollam</option>
  </select>
  {extra_select}
  <table id="GridMeetingDEtails">
    <tr><td>row1</td></tr>
    {pager_html}
  </table>
</form>
</body></html>
"""


def _post_response_html(value_for_district: str = "2") -> str:
    return _dashboard_html(district_value=value_for_district)


# ---------------------------------------------------------------------------
# load_page
# ---------------------------------------------------------------------------
@responses.activate
def test_load_page_parses_form_state(client: SakarmaClient) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)

    state = client.load_page(LBWISE_PATH)

    assert state.viewstate == "VS_TOKEN"
    assert state.viewstate_generator == "VSG_TOKEN"
    assert state.event_validation == "EV_TOKEN"
    assert state.page_url == url
    # Default selected district must be in form_fields.
    assert state.form_fields[DDL_DISTRICT] == "1"


# ---------------------------------------------------------------------------
# select_dropdown
# ---------------------------------------------------------------------------
@responses.activate
def test_select_dropdown_posts_event_target_and_value(client: SakarmaClient) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)
    responses.add(responses.POST, url, body=_post_response_html("2"), status=200)

    state = client.load_page(LBWISE_PATH)
    new_state = client.select_dropdown(state, DDL_DISTRICT, "2")

    post_call = responses.calls[1]
    assert "__EVENTTARGET=ctl00%24ContentPlaceHolder1%24ddlDistrict" in post_call.request.body
    # The new value is echoed in the POST body as the dropdown's own field.
    assert "ctl00%24ContentPlaceHolder1%24ddlDistrict=2" in post_call.request.body
    assert new_state.viewstate == "VS_TOKEN"


# ---------------------------------------------------------------------------
# click_button
# ---------------------------------------------------------------------------
@responses.activate
def test_click_button_returns_new_state(client: SakarmaClient) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)
    responses.add(responses.POST, url, body=_dashboard_html(), status=200)

    state = client.load_page(LBWISE_PATH)
    new_state = client.click_button(state, BTN_APPV_MEETINGS)

    assert new_state.viewstate == "VS_TOKEN"
    assert (
        "__EVENTTARGET=ctl00%24ContentPlaceHolder1%24btnAppv_Meetings"
        in responses.calls[1].request.body
    )


# ---------------------------------------------------------------------------
# select_grid_row + fetch_public_page
# ---------------------------------------------------------------------------
@responses.activate
def test_select_grid_row_then_fetch_public_minutes(client: SakarmaClient) -> None:
    dash_url = client._abs(LBWISE_PATH)
    minutes_url = client._abs(PUBLIC_MINUTES_PATH)
    minutes_body = "<html><body>RAW_MINUTES_BYTES_àé</body></html>".encode(
        "utf-8"
    )

    responses.add(responses.GET, dash_url, body=_dashboard_html(), status=200)
    responses.add(responses.POST, dash_url, body=_dashboard_html(), status=200)
    responses.add(responses.GET, minutes_url, body=minutes_body, status=200)

    state = client.load_page(LBWISE_PATH)
    client.select_grid_row(state, 0)
    raw = client.fetch_public_page(PUBLIC_MINUTES_PATH)

    assert raw == minutes_body  # bytes preserved verbatim
    grid_post = responses.calls[1].request.body
    assert "__EVENTTARGET=ctl00%24ContentPlaceHolder1%24GridMeetingDEtails" in grid_post
    assert "__EVENTARGUMENT=Select%240" in grid_post


# ---------------------------------------------------------------------------
# click_attachment_lnkfileview
# ---------------------------------------------------------------------------
@responses.activate
def test_click_attachment_returns_bytes_and_filename(client: SakarmaClient) -> None:
    url = client._abs(LBWISE_PATH)
    pdf_bytes = b"%PDF-1.7\n%fake-content\n%%EOF"
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)
    responses.add(
        responses.POST,
        url,
        body=pdf_bytes,
        status=200,
        headers={"Content-Disposition": 'attachment; filename="decision_42.pdf"'},
    )

    state = client.load_page(LBWISE_PATH)
    body, filename = client.click_attachment_lnkfileview(
        state, decision_attachment_target(0)
    )

    assert body == pdf_bytes
    assert filename == "decision_42.pdf"


@responses.activate
def test_click_attachment_missing_disposition_returns_empty_filename(
    client: SakarmaClient,
) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)
    responses.add(responses.POST, url, body=b"%PDF-1.7\n%%EOF", status=200)

    state = client.load_page(LBWISE_PATH)
    body, filename = client.click_attachment_lnkfileview(
        state, decision_attachment_target(0)
    )

    assert body.startswith(b"%PDF")
    assert filename == ""


# ---------------------------------------------------------------------------
# Edge: session-expiry detection
# ---------------------------------------------------------------------------
@responses.activate
def test_postback_without_viewstate_raises_session_expired(
    client: SakarmaClient,
) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)
    # Postback response is a login page or empty — no __VIEWSTATE field.
    responses.add(
        responses.POST,
        url,
        body="<html><body>Please log in</body></html>",
        status=200,
    )

    state = client.load_page(LBWISE_PATH)
    with pytest.raises(SessionExpiredError):
        client.select_dropdown(state, DDL_DISTRICT, "2")


# ---------------------------------------------------------------------------
# Edge: pagination detection on drill-down
# ---------------------------------------------------------------------------
@responses.activate
def test_click_button_raises_on_pagination(client: SakarmaClient) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)
    responses.add(
        responses.POST, url, body=_dashboard_html(pager=True), status=200
    )

    state = client.load_page(LBWISE_PATH)
    with pytest.raises(PaginationDetectedError) as exc_info:
        client.click_button(state, BTN_APPV_MEETINGS)
    msg = str(exc_info.value)
    assert "category=" in msg and "url=" in msg


# ---------------------------------------------------------------------------
# Error: 5xx triggers retry, succeeds eventually
# ---------------------------------------------------------------------------
@responses.activate
def test_503_retries_then_succeeds(client: SakarmaClient) -> None:
    url = client._abs(LBWISE_PATH)
    responses.add(responses.GET, url, status=503, body="boom")
    responses.add(responses.GET, url, status=503, body="boom")
    responses.add(responses.GET, url, body=_dashboard_html(), status=200)

    state = client.load_page(LBWISE_PATH)
    assert state.viewstate == "VS_TOKEN"
    assert len(responses.calls) == 3


# ---------------------------------------------------------------------------
# Integration-ish: full sequence with mocks
# ---------------------------------------------------------------------------
@responses.activate
def test_full_sequence_load_select_click_grid_fetch(client: SakarmaClient) -> None:
    dash_url = client._abs(LBWISE_PATH)
    minutes_url = client._abs(PUBLIC_MINUTES_PATH)
    expected_minutes = b"<html><body>MINUTES OK</body></html>"

    # 1. GET dashboard
    responses.add(responses.GET, dash_url, body=_dashboard_html(), status=200)
    # 2. POST select_dropdown
    responses.add(responses.POST, dash_url, body=_dashboard_html(), status=200)
    # 3. POST click_button (drill-down)
    responses.add(responses.POST, dash_url, body=_dashboard_html(), status=200)
    # 4. POST select_grid_row
    responses.add(responses.POST, dash_url, body=_dashboard_html(), status=200)
    # 5. GET PublicMinutes
    responses.add(responses.GET, minutes_url, body=expected_minutes, status=200)

    state = client.load_page(LBWISE_PATH)
    state = client.select_dropdown(state, DDL_DISTRICT, "2")
    state = client.click_button(state, BTN_APPV_MEETINGS)
    client.select_grid_row(state, 0)
    raw = client.fetch_public_page(PUBLIC_MINUTES_PATH)

    assert raw == expected_minutes
    assert len(responses.calls) == 5


# ---------------------------------------------------------------------------
# parse_form_state direct tests
# ---------------------------------------------------------------------------
def test_parse_form_state_picks_first_option_when_none_selected() -> None:
    html = _dashboard_html().replace(
        '<option value="1" selected>', '<option value="1">'
    ).encode("utf-8")
    state = parse_form_state(html, page_url="https://example/x")
    # Falls back to the first option (value=0)
    assert state.form_fields[DDL_DISTRICT] == "0"
    assert isinstance(state, FormState)


def test_grid_dr_target_indexing() -> None:
    assert grid_dr_target(0).endswith("$ctl02$lnkDR")
    assert grid_dr_target(1).endswith("$ctl03$lnkDR")
    assert grid_dr_target(8).endswith("$ctl10$lnkDR")
