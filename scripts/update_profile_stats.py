#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://api.github.com"
GRAPHQL_URL = f"{API_URL}/graphql"
USER_API_URL = f"{API_URL}/user"
UTC = timezone.utc
COUNTING_START = date(2026, 1, 1)


def clean_token(value: str) -> str:
    token = "".join(value.split())
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in token):
        raise RuntimeError("O token contém caracteres de controle inválidos.")
    return token


def api_json(token: str, url: str, *, data: dict | None = None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="GET" if body is None else "POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bielxdh3-profile-contribution-graph",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8")), response.headers
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Falha ao acessar a API do GitHub: {exc}") from exc


def graphql(token: str, query: str, variables: dict) -> dict:
    body, _ = api_json(token, GRAPHQL_URL, data={"query": query, "variables": variables})
    errors = body.get("errors") or []
    if errors:
        raise RuntimeError(
            f"GitHub GraphQL retornou erros: {json.dumps(errors, ensure_ascii=False)}"
        )
    data = body.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Resposta GraphQL do GitHub sem campo data válido.")
    return data


def validate_private_token(token: str, expected_login: str) -> None:
    body, headers = api_json(token, USER_API_URL)
    login = str(body.get("login", ""))
    if login.lower() != expected_login.lower():
        raise RuntimeError(
            f"PROFILE_STATS_TOKEN pertence a @{login or 'desconhecido'}, não a @{expected_login}."
        )

    scopes_header = headers.get("X-OAuth-Scopes", "")
    scopes = {item.strip() for item in scopes_header.split(",") if item.strip()}
    missing = {"repo", "read:user"} - scopes
    if missing:
        raise RuntimeError(
            "PROFILE_STATS_TOKEN precisa ser PAT classic com repo e read:user. "
            f"Faltando: {', '.join(sorted(missing))}."
        )


def fetch_contribution_window(
    token: str,
    login: str,
    start: datetime,
    end: datetime,
) -> tuple[dict[date, int], int]:
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    variables = {
        "login": login,
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": end.isoformat().replace("+00:00", "Z"),
    }
    data = graphql(token, query, variables)
    user = data.get("user")
    if not user:
        raise RuntimeError(f"Usuário @{login} não encontrado no GitHub.")

    calendar = user["contributionsCollection"]["contributionCalendar"]
    expected_total = int(calendar["totalContributions"])
    start_day = start.date()
    end_day = end.date()
    daily: dict[date, int] = {}

    for week in calendar["weeks"]:
        for node in week["contributionDays"]:
            day = date.fromisoformat(str(node["date"]))
            if start_day <= day <= end_day:
                daily[day] = int(node["contributionCount"])

    observed_total = sum(daily.values())
    if observed_total != expected_total:
        raise RuntimeError(
            "O total do calendário do perfil e a soma diária divergiram: "
            f"total={expected_total}, dias={observed_total}."
        )

    return daily, expected_total


def fetch_profile_contributions_since_2026(
    token: str, login: str
) -> tuple[dict[date, int], int, date]:
    now = datetime.now(UTC)
    today = now.date()
    if today < COUNTING_START:
        return {}, 0, today

    daily: dict[date, int] = {}
    total = 0

    for year in range(COUNTING_START.year, today.year + 1):
        start_day = COUNTING_START if year == COUNTING_START.year else date(year, 1, 1)
        start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=UTC)
        end = now if year == today.year else datetime(year, 12, 31, 23, 59, 59, tzinfo=UTC)

        period_daily, period_total = fetch_contribution_window(token, login, start, end)
        total += period_total
        daily.update(period_daily)

    if sum(daily.values()) != total:
        raise RuntimeError("Falha interna ao consolidar as contribuições desde 2026.")

    return daily, total, today


def fill_daily_series(
    daily_counts: dict[date, int], last_day: date
) -> tuple[list[dict[str, object]], date | None]:
    active_days = sorted(
        day for day, count in daily_counts.items()
        if day >= COUNTING_START and count > 0
    )
    if not active_days:
        return [], None

    first_day = active_days[0]
    current = first_day
    running = 0
    series: list[dict[str, object]] = []

    while current <= last_day:
        count = daily_counts.get(current, 0)
        running += count
        series.append(
            {
                "date": current.isoformat(),
                "contributions": count,
                "total": running,
            }
        )
        current = date.fromordinal(current.toordinal() + 1)

    return series, first_day


def format_pt(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def render_svg(
    login: str,
    total: int,
    series: list[dict[str, object]],
    include_private: bool,
) -> str:
    width, height = 900, 320
    left, right, top, bottom = 92, 40, 90, 56
    plot_w = width - left - right
    plot_h = height - top - bottom

    totals = [int(item["total"]) for item in series] or [total]
    y_min_raw = min(totals)
    y_max_raw = max(totals)

    if y_min_raw == y_max_raw:
        pad = max(5, math.ceil(max(1, y_max_raw) * 0.01))
    else:
        pad = max(3, math.ceil((y_max_raw - y_min_raw) * 0.08))
    y_min = max(0, y_min_raw - pad)
    y_max = y_max_raw + pad
    y_span = max(1, y_max - y_min)

    def x_for(index: int) -> float:
        if len(series) <= 1:
            return left + plot_w / 2
        return left + (index / (len(series) - 1)) * plot_w

    def y_for(value: int) -> float:
        return top + (1 - ((value - y_min) / y_span)) * plot_h

    points = [(x_for(i), y_for(int(item["total"]))) for i, item in enumerate(series)]
    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

    area_path = ""
    latest_x, latest_y = left + plot_w / 2, top + plot_h / 2
    if points:
        area_path = (
            f"M {points[0][0]:.2f} {top + plot_h:.2f} "
            + " ".join(f"L {x:.2f} {y:.2f}" for x, y in points)
            + f" L {points[-1][0]:.2f} {top + plot_h:.2f} Z"
        )
        latest_x, latest_y = points[-1]

    y_ticks = []
    for i in range(5):
        ratio = i / 4
        value = round(y_max - ratio * y_span)
        y = top + ratio * plot_h
        y_ticks.append((value, y))

    x_tick_indices: list[int] = []
    if series:
        for ratio in (0, 0.25, 0.5, 0.75, 1):
            idx = round((len(series) - 1) * ratio)
            if idx not in x_tick_indices:
                x_tick_indices.append(idx)

    grid = "\n".join(
        f'''  <line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#27303A" stroke-width="1"/>
  <text x="{left - 14}" y="{y + 4:.2f}" text-anchor="end" class="axis">{format_pt(value)}</text>'''
        for value, y in y_ticks
    )

    x_labels = []
    for idx in x_tick_indices:
        day = date.fromisoformat(str(series[idx]["date"]))
        x_labels.append(
            f'  <text x="{x_for(idx):.2f}" y="{top + plot_h + 34}" text-anchor="middle" class="axis">{day.strftime("%d/%m/%y")}</text>'
        )

    line_markup = ""
    if points:
        line_markup = f'''
  <path d="{area_path}" fill="url(#areaGradient)"/>
  <polyline points="{polyline}" fill="none" stroke="url(#lineGradient)" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="{latest_x:.2f}" cy="{latest_y:.2f}" r="6" fill="#C8B6FF" stroke="#B8E6FF" stroke-width="3"/>
'''

    first_date = series[0]["date"] if series else ""
    last_date = series[-1]["date"] if series else ""
    first_label = date.fromisoformat(str(first_date)).strftime("%d/%m/%Y") if first_date else ""
    private_note = (
        "repositórios privados permanecem anônimos"
        if include_private
        else "somente contribuições públicas visíveis"
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="320" viewBox="0 0 900 320" role="img" aria-labelledby="title desc">
  <title id="title">Evolução das contribuições de {login}</title>
  <desc id="desc">Contribuições contabilizadas pelo perfil GitHub do primeiro registro de 2026, em {first_date}, até {last_date}. Total: {total}. {private_note}.</desc>
  <defs>
    <linearGradient id="lineGradient" x1="0" x2="1">
      <stop offset="0%" stop-color="#B8E6FF"/>
      <stop offset="55%" stop-color="#9ED8FA"/>
      <stop offset="100%" stop-color="#C8B6FF"/>
    </linearGradient>
    <linearGradient id="areaGradient" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#B8E6FF" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="#C8B6FF" stop-opacity="0.02"/>
    </linearGradient>
    <style>
      .title {{ fill: #E6EDF3; font: 700 22px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
      .value {{ fill: #B8E6FF; font: 700 22px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
      .subtitle {{ fill: #8B949E; font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
      .axis {{ fill: #8B949E; font: 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
      .small {{ fill: #8B949E; font: 10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    </style>
  </defs>

  <rect x="1" y="1" width="898" height="318" rx="18" fill="#0D1117" stroke="#30363D"/>
  <text x="42" y="38" class="title">EVOLUÇÃO DAS CONTRIBUIÇÕES</text>
  <text x="858" y="38" text-anchor="end" class="value">{format_pt(total)}</text>
  <text x="42" y="60" class="subtitle">desde {first_label} · {private_note}</text>
  <text x="858" y="60" text-anchor="end" class="small">@{login}</text>

{grid}
  <line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#3A4550" stroke-width="1.2"/>
{line_markup}
{chr(10).join(x_labels)}
</svg>
'''


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atualiza o gráfico de contribuições a partir da primeira contribuição de 2026."
    )
    parser.add_argument("--user", default=os.environ.get("PROFILE_USER", "bielxdh3"))
    parser.add_argument("--svg", default="assets/commit-history.svg")
    parser.add_argument("--data", default="data/commit-history.json")
    args = parser.parse_args()

    private_token = clean_token(os.environ.get("PROFILE_STATS_TOKEN", ""))
    workflow_token = clean_token(os.environ.get("GITHUB_TOKEN", ""))
    include_private = bool(private_token)

    if include_private:
        validate_private_token(private_token, args.user)
        token = private_token
    elif workflow_token:
        token = workflow_token
    else:
        raise SystemExit("Defina GITHUB_TOKEN ou PROFILE_STATS_TOKEN.")

    daily_counts, github_total, window_end = fetch_profile_contributions_since_2026(
        token, args.user
    )
    series, first_day = fill_daily_series(daily_counts, window_end)

    if series and int(series[-1]["total"]) != github_total:
        raise RuntimeError(
            f"O acumulado final ({series[-1]['total']}) não corresponde ao total desde 2026 ({github_total})."
        )

    payload = {
        "schema": 8,
        "user": args.user,
        "generated_at": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "scope": "github_profile_contributions_from_first_2026_activity",
        "includes_private_contributions": include_private,
        "private_repository_details_published": False,
        "counting_method": "GitHub GraphQL contributionCalendar.totalContributions by yearly windows",
        "counting_window_start": COUNTING_START.isoformat(),
        "graph_window_start": first_day.isoformat() if first_day else None,
        "graph_window_end": window_end.isoformat(),
        "first_contribution_date": first_day.isoformat() if first_day else None,
        "total_contributions": github_total,
        "series": series,
    }

    svg_path = Path(args.svg)
    data_path = Path(args.data)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(
        render_svg(args.user, github_total, series, include_private), encoding="utf-8"
    )
    data_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    first_text = first_day.strftime("%d/%m/%Y") if first_day else "nenhuma contribuição"
    print(
        f"Atualizado: {format_pt(github_total)} contribuições; gráfico iniciado em {first_text}."
    )


if __name__ == "__main__":
    main()
