#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GRAPHQL_URL = "https://api.github.com/graphql"
UTC = timezone.utc


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_github_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def graphql(token: str, query: str, variables: dict[str, object]) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = Request(
        GRAPHQL_URL,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "bielxdh3-profile-commit-graph",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Falha ao acessar o GitHub GraphQL: {exc}") from exc

    if body.get("errors"):
        raise RuntimeError(
            f"GitHub GraphQL retornou erros: {json.dumps(body['errors'], ensure_ascii=False)}"
        )
    return body["data"]


def get_created_at(token: str, login: str) -> datetime:
    query = '''
    query($login: String!) {
      user(login: $login) {
        createdAt
      }
    }
    '''
    data = graphql(token, query, {"login": login})
    user = data.get("user")
    if not user:
        raise RuntimeError(f"Usuário GitHub não encontrado: {login}")
    return parse_github_datetime(user["createdAt"])


def get_interval_total(token: str, login: str, start: datetime, end: datetime) -> int:
    query = '''
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
        }
      }
    }
    '''
    data = graphql(
        token,
        query,
        {"login": login, "from": iso_z(start), "to": iso_z(end)},
    )
    return int(data["user"]["contributionsCollection"]["totalCommitContributions"])


def find_first_commit_day(token: str, login: str, created_at: datetime, now: datetime) -> date:
    """Localiza o primeiro dia com commit público reconhecido pelo GitHub.

    Primeiro percorre a vida da conta em blocos de até 180 dias. Quando encontra
    o primeiro bloco com commits, faz uma busca binária para achar o primeiro dia
    real. Assim o gráfico não começa na criação da conta nem em uma janela fixa.
    """
    cursor = datetime.combine(created_at.date(), time.min, tzinfo=UTC)

    while cursor <= now:
        interval_end = min(cursor + timedelta(days=180) - timedelta(seconds=1), now)
        if get_interval_total(token, login, cursor, interval_end) > 0:
            low = cursor.date()
            high = interval_end.date()

            while low < high:
                midpoint = low + timedelta(days=(high - low).days // 2)
                midpoint_end = min(
                    datetime.combine(midpoint, time(23, 59, 59), tzinfo=UTC),
                    now,
                )
                if get_interval_total(token, login, cursor, midpoint_end) > 0:
                    high = midpoint
                else:
                    low = midpoint + timedelta(days=1)

            return low

        cursor = interval_end + timedelta(seconds=1)

    return now.date()


def get_daily_totals(token: str, login: str, start_day: date, end_day: date) -> dict[date, int]:
    days: list[date] = []
    current = start_day
    while current <= end_day:
        days.append(current)
        current += timedelta(days=1)

    result: dict[date, int] = {day: 0 for day in days}

    # Vários contributionsCollection com aliases em uma única query.
    # Lotes de 28 dias mantêm custo e tamanho da query previsíveis.
    for offset in range(0, len(days), 28):
        batch = days[offset : offset + 28]
        fields = []
        for index, day in enumerate(batch):
            start = datetime.combine(day, time.min, tzinfo=UTC)
            end = datetime.combine(day, time(23, 59, 59), tzinfo=UTC)
            fields.append(
                f'''d{index}: contributionsCollection(
                  from: "{iso_z(start)}",
                  to: "{iso_z(end)}"
                ) {{
                  totalCommitContributions
                }}'''
            )

        query = (
            "query($login: String!) {\n"
            "  user(login: $login) {\n"
            + "\n".join("    " + field.replace("\n", "\n    ") for field in fields)
            + "\n  }\n}"
        )
        data = graphql(token, query, {"login": login})
        user = data["user"]
        for index, day in enumerate(batch):
            result[day] = int(user[f"d{index}"]["totalCommitContributions"])

    return result


def format_pt(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def build_series(daily_counts: dict[date, int]) -> list[dict[str, object]]:
    running = 0
    series: list[dict[str, object]] = []
    for day in sorted(daily_counts):
        running += daily_counts[day]
        series.append(
            {
                "date": day.isoformat(),
                "commits": daily_counts[day],
                "total": running,
            }
        )
    return series


def render_svg(login: str, lifetime_total: int, series: list[dict[str, object]]) -> str:
    width, height = 900, 320
    left, right, top, bottom = 92, 40, 90, 56
    plot_w = width - left - right
    plot_h = height - top - bottom

    totals = [int(item["total"]) for item in series] or [lifetime_total]
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

    def y_for(total: int) -> float:
        return top + (1 - ((total - y_min) / y_span)) * plot_h

    points = [(x_for(i), y_for(int(item["total"]))) for i, item in enumerate(series)]
    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)

    if points:
        area_path = (
            f"M {points[0][0]:.2f} {top + plot_h:.2f} "
            + " ".join(f"L {x:.2f} {y:.2f}" for x, y in points)
            + f" L {points[-1][0]:.2f} {top + plot_h:.2f} Z"
        )
        latest_x, latest_y = points[-1]
    else:
        area_path = ""
        latest_x, latest_y = left + plot_w / 2, top + plot_h / 2

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
        x = x_for(idx)
        x_labels.append(
            f'  <text x="{x:.2f}" y="{top + plot_h + 34}" text-anchor="middle" class="axis">{day.strftime("%d/%m/%y")}</text>'
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

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="320" viewBox="0 0 900 320" role="img" aria-labelledby="title desc">
  <title id="title">Evolução dos commits públicos de {login}</title>
  <desc id="desc">Gráfico acumulado somente dos commits públicos reconhecidos pelo GitHub, do primeiro commit público em {first_date} até {last_date}. Total atual: {lifetime_total}.</desc>
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
  <text x="42" y="38" class="title">EVOLUÇÃO DOS COMMITS PÚBLICOS</text>
  <text x="858" y="38" text-anchor="end" class="value">{format_pt(lifetime_total)}</text>
  <text x="42" y="60" class="subtitle">somente commits públicos · desde o primeiro commit público reconhecido pelo GitHub</text>
  <text x="858" y="60" text-anchor="end" class="small">@{login}</text>

{grid}
  <line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#3A4550" stroke-width="1.2"/>
{line_markup}
{chr(10).join(x_labels)}
</svg>
'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Atualiza o gráfico público de commits do README de perfil.")
    parser.add_argument("--user", default=os.environ.get("PROFILE_USER", "bielxdh3"))
    parser.add_argument("--svg", default="assets/commit-history.svg")
    parser.add_argument("--data", default="data/commit-history.json")
    args = parser.parse_args()

    # O workflow usa somente github.token do repositório público de perfil.
    # Não há fallback para PAT com acesso a repositórios privados.
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("Defina GITHUB_TOKEN.")

    now = datetime.now(UTC).replace(microsecond=0)
    created_at = get_created_at(token, args.user)
    first_commit_day = find_first_commit_day(token, args.user, created_at, now)

    today = now.date()
    daily_counts = get_daily_totals(token, args.user, first_commit_day, today)
    lifetime_total = sum(daily_counts.values())
    series = build_series(daily_counts)

    payload = {
        "schema": 2,
        "user": args.user,
        "generated_at": iso_z(now),
        "scope": "public commit contributions visible to the repository-scoped GitHub Actions token",
        "public_only": True,
        "first_public_commit_date": first_commit_day.isoformat(),
        "total_public_commit_contributions": lifetime_total,
        "series": series,
    }

    svg_path = Path(args.svg)
    data_path = Path(args.data)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)

    svg_path.write_text(render_svg(args.user, lifetime_total, series), encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Atualizado: {format_pt(lifetime_total)} commits públicos contabilizados pelo GitHub.")


if __name__ == "__main__":
    main()
