"""Each page in a real browser: it renders its data, and nothing on it throws, logs an error or gets a 5xx.

The demo mesh has 25 made-up radios, 3 stations and 3 days of traffic (demo.py), so every view has something to show.
"""
import re
import tomllib

import pytest
from playwright.sync_api import expect

PAGES = ["/", "/analytics.html", "/visualizations.html", "/packets.html"]
LOCAL_PAGES = ["/settings.html", "/radio.html", "/stations.html"]


def clean(page):
    assert page.problems == [], "\n".join(page.problems)


def test_map_lists_the_radios_and_opens_one(page, demo_url):
    page.goto(demo_url + "/")
    rows = page.locator("#nodelist li[data-id]")
    expect(rows.first).to_be_visible()
    assert rows.count() >= 20
    expect(page.locator(".leaflet-marker-icon, .leaflet-interactive").first).to_be_attached()
    rows.nth(3).click()
    expect(page.locator('a[href^="/analytics.html#node="]')).to_be_visible()  # the node panel's Analytics link
    clean(page)


def test_analytics_overview_and_a_node_page(page, demo_url):
    page.goto(demo_url + "/analytics.html#overview")
    expect(page.locator("#kpis")).to_contain_text("Packets heard")
    page.locator("#tabbtn-radios").click()            # the radios table lives on the Radios tab
    rows = page.locator("#nodes tbody tr")
    expect(rows.first).to_be_visible()
    assert rows.count() >= 20
    expect(page.locator("#traffic svg").first).to_be_attached()
    rows.nth(2).click()  # a row opens that radio's page
    expect(page.locator("#nodeView")).to_be_visible()
    assert "#node=" in page.url
    expect(page.locator("#nodeView svg").first).to_be_attached()
    clean(page)


def test_analytics_tabs_load_only_what_they_show(page, demo_url):
    asked = []
    page.on("request", lambda r: asked.append(r.url) if "/api/" in r.url else None)
    page.goto(demo_url + "/analytics.html#overview")
    expect(page.locator("#traffic svg").first).to_be_attached(timeout=15_000)
    expect(page.locator("#tab-network")).to_be_hidden()
    heavy = ("/api/analytics/topology", "/api/analytics/critical", "/api/analytics/links", "/api/analytics/airtime",
             "/api/insights/")
    # Overview runs only the main summary, plus its own "Unusual right now" check
    assert not [u for u in asked if any(h in u for h in heavy) and "/api/insights/anomalies" not in u]
    expect(page.locator("#anomBody")).not_to_contain_text("Checking", timeout=15_000)
    for tab, el in (("network", "#critSummary"), ("channel", "#airSummary"), ("health", "#healthSummary"), ("radios", "#nodes tbody tr")):
        page.locator(f"#tabbtn-{tab}").click()
        assert page.url.endswith(f"#{tab}")
        expect(page.locator(f"#tab-{tab}")).to_be_visible()
        expect(page.locator(el).first).not_to_be_empty(timeout=30_000)
    page.reload()                                       # the open tab comes back after a reload
    expect(page.locator("#tab-radios")).to_be_visible()
    clean(page)


def test_link_history_card_and_compare(page, demo_url):
    page.goto(demo_url + "/analytics.html#links")
    expect(page.locator("#linkSummary")).to_contain_text("link", timeout=15_000)
    expect(page.locator("#linkTable .lk-spark").first).to_be_attached()
    expect(page.locator("#linkCompare")).to_have_attribute("open", "")      # #links opens the comparison
    with page.expect_response(lambda r: "/api/analytics/links/compare" in r.url, timeout=30_000):
        page.locator("#lcForm button[type=submit]").click()
    expect(page.locator("#lcOut table")).to_be_visible(timeout=15_000)
    expect(page.locator("#critHistSummary")).not_to_be_empty(timeout=30_000)     # critical relays over time
    expect(page.locator("#critHistSummary")).not_to_contain_text("Couldn't")
    clean(page)


def test_visualizations_graph_map_and_replay(page, demo_url):
    page.goto(demo_url + "/visualizations.html")
    graph = page.locator("#topoReplayGraph svg circle")
    expect(graph.first).to_be_attached(timeout=20000)
    assert graph.count() >= 20
    page.locator('#topoMode button[data-mode="geo"]').click()
    expect(page.locator(".leaflet-container").first).to_be_visible()
    page.locator("#replayPlay").click()
    page.wait_for_timeout(1500)
    expect(page.locator("#replayClock")).not_to_be_empty()
    clean(page)


def test_visualizations_show_menu_filters_and_colours(page, demo_url):
    page.goto(demo_url + "/visualizations.html")
    nodes = page.locator("#topoReplayGraph g.node")
    expect(nodes.first).to_be_attached(timeout=20000)
    everyone = nodes.count()
    page.locator("#vzShow summary").click()
    page.select_option("#vfRole", "routers")                       # routers and repeaters (and the station)
    expect(page.locator("#vzShowCount")).to_have_text("1")
    page.wait_for_function(f"document.querySelectorAll('#topoReplayGraph g.node').length < {everyone}", timeout=20000)
    # the figures arrive slowly (as on a slow CI machine, which found the race) while the graph is redrawn meanwhile
    page.route("**/api/analytics/critical*", lambda route: (page.wait_for_timeout(1500), route.continue_()))
    page.select_option("#vfColour", "cuts")                        # single points of failure in the warning colour
    page.evaluate("redraw(); setTimeout(redraw, 300)")
    page.wait_for_function("[...document.querySelectorAll('#topoReplayGraph circle')].some(c => "
                           "(c.getAttribute('fill') || '').includes('--warning'))", timeout=20000)
    page.locator("#vfReset").click()
    page.wait_for_function(f"document.querySelectorAll('#topoReplayGraph g.node').length >= {everyone}", timeout=20000)
    clean(page)


def test_packets_table_and_anatomy(page, demo_url):
    # Waits on the two requests the page makes rather than on fixed timeouts: under a full run the demo server is
    # slow, and a click on a row before the list arrived, or a count taken mid-render, failed now and then.
    with page.expect_response(lambda r: "/api/packets/search" in r.url, timeout=30_000):
        page.goto(demo_url + "/packets.html")
    rows = page.locator("#ptable tbody tr.has-raw")
    expect(rows.first).to_be_visible(timeout=15_000)
    with page.expect_response(lambda r: "/anatomy" in r.url, timeout=30_000):
        rows.first.click()
    anat = page.locator(".anat-row .anat-layer")
    expect(anat.nth(2)).to_be_visible(timeout=15_000)  # radio, header, encryption...
    clean(page)


@pytest.mark.parametrize("path", PAGES)
def test_no_sideways_scrolling_on_a_phone(browser, demo_url, path):
    ctx = browser.new_context(viewport={"width": 375, "height": 812}, is_mobile=True)
    pg = ctx.new_page()
    pg.goto(demo_url + path)
    pg.wait_for_timeout(2500)
    over = pg.evaluate("() => document.documentElement.scrollWidth - innerWidth")
    ctx.close()
    assert over <= 1, f"{path} is {over}px wider than a 375px phone"


def test_demo_hides_this_computers_pages(page, demo_url):
    page.goto(demo_url + "/")
    expect(page.locator(".rotag.demo")).to_be_visible()
    nav = page.locator(".pagenav")
    expect(nav).not_to_contain_text("Radio")
    expect(nav).not_to_contain_text("Stations")
    clean(page)


@pytest.mark.parametrize("path", LOCAL_PAGES)
def test_this_computers_pages_load_and_link_each_other(page, local, path):
    url, _ = local
    page.goto(url + path)
    page.wait_for_timeout(1500)
    nav = page.locator(".pagenav")
    for name in ("Map", "Analytics", "Visualizations", "Packets", "Settings", "Radio", "Stations"):
        expect(nav.get_by_role("link", name=name, exact=True)).to_have_count(1)
    clean(page)


def test_radio_page_without_a_radio(page, local):
    url, _ = local
    page.goto(url + "/radio.html")
    expect(page.locator("#connect")).to_contain_text("No radio connected")
    clean(page)


def test_settings_saves_to_the_file_and_marks_the_restart(page, local):
    url, cfg = local
    page.goto(url + "/settings.html")
    zoom = page.locator('[data-k="toml.map.zoom"]')
    zoom.fill("13")
    silence = page.locator('[data-k="alerts.silenceHours"]')
    silence.fill("5")
    expect(page.locator("#saveCount")).to_have_text("2 changes not saved yet")
    page.locator("#saveBtn").click()
    expect(page.locator("#saveNote")).to_contain_text("Saved")
    assert tomllib.loads(cfg.read_text(encoding="utf-8"))["map"]["zoom"] == 13
    expect(page.locator('[data-row="toml.map.zoom"] .restarttag')).to_be_visible()
    expect(page.locator("#restartBanner")).to_contain_text("Map: zoomed to")
    page.reload()
    expect(page.locator('[data-k="alerts.silenceHours"]')).to_have_value("5")  # applied at once
    page.locator('[data-k="toml.backup.time"]').fill("25:00")  # refused like a hand edit; the file is untouched
    page.locator("#saveBtn").click()
    expect(page.locator("#err")).to_contain_text("HH:MM")
    assert "25:00" not in cfg.read_text(encoding="utf-8")
    page.problems = [p for p in page.problems if "status of 400" not in p]  # that refusal is the browser's "error"
    clean(page)


def test_diagnostics_preview_and_download(page, local):
    url, _ = local
    page.goto(url + "/settings.html#help")
    page.locator("#diagPreview").click()
    files = page.locator("#diagFiles details.diag")
    expect(files.first).to_be_visible(timeout=20000)
    expect(page.locator("#diagFiles")).to_contain_text("summary.json")
    expect(page.locator("#diagFiles")).to_contain_text('"running": true')
    with page.expect_download() as dl:
        page.locator("#diagSave").click()
    assert dl.value.suggested_filename.startswith("lorakeet-diagnostics-")
    clean(page)


def test_the_bell_lists_alerts_and_links_to_settings(page, local):
    url, _ = local
    page.goto(url + "/analytics.html")
    page.locator("#bell").click()
    pop = page.locator("#alertpop")
    expect(pop).to_be_visible()
    pop.get_by_role("link", name="Settings").click()
    expect(page).to_have_url(re.compile(r"/settings\.html#alerts$"))
    clean(page)


LIT_PIXELS = """() => { const c = document.querySelector('.groovy-front'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
  let n = 0; for (let i = 3; i < d.length; i += 64) if (d[i] > 10) n++; return n; }"""


def test_groovy_mode_draws_on_graph_and_map_and_turns_off(page, demo_url):
    page.goto(demo_url + "/visualizations.html?groovy=neon")
    expect(page.locator("#vzGroovy")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#topoReplayGraph svg .edge").first).to_be_attached(timeout=20000)
    page.locator("#replayPlay").click()
    page.wait_for_timeout(2500)
    assert page.evaluate(LIT_PIXELS) > 0, "sparks and trails over the graph"
    stroke = page.evaluate("() => document.querySelector('#topoReplayGraph .edge').style.stroke")
    assert stroke.startswith(("hsl", "rgb")), "links recoloured"
    page.locator('#topoMode button[data-mode="geo"]').click()  # a redraw mid-flight once stopped the effects for good
    page.wait_for_timeout(3000)
    if page.locator("#replayPlay").inner_text().strip().lower().startswith("play"):
        page.locator("#replayPlay").click()
    page.wait_for_timeout(2500)
    assert page.evaluate(LIT_PIXELS) > 0, "still drawing over the map"
    page.keyboard.press("v")
    expect(page.locator("#vzGroovy")).to_have_attribute("aria-pressed", "false")
    expect(page.locator(".groovy-front")).to_be_hidden()
    clean(page)
