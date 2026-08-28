#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_URL = "https://api.github.com"
USER_API_URL = f"{API_URL}/user"
UTC = timezone.utc


def clean_token(value: str) -> str:
    token = "".join(value.split())
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in token):
        raise RuntimeError("O token contém caracteres de controle inválidos.")
    return token


def api_get(token: str, url: str, *, allow_empty_repo: bool = False):
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bielxdh3-profile-commit-graph",
        },
    )
    try:
        with urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8")), response.headers
    except HTTPError as exc:
        if allow_empty_repo and exc.code in {404, 409}:
            return [], exc.headers
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"Falha ao acessar a API do GitHub: {exc}") from exc


def validate_private_token(token: str, expected_login: str) -> None:
    body, headers = api_get(token, USER_API_URL)
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


def paged_list(token: str, base_url: str, *, allow_empty_repo: bool = False) -> list[dict]:
    results: list[dict] = []
    page = 1
    separator = "&" if "?" in base_url else "?"
    while True:
        url = f"{base_url}{separator}per_page=100&page={page}"
        batch, _ = api_get(token, url, allow_empty_repo=allow_empty_repo)
        if not isinstance(batch, list):
            raise RuntimeError(f"Resposta inesperada da API em {url}")
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return results


def list_repositories(token: str, login: str, include_private: bool) -> list[dict]:
    if include_private:
        query = urlencode(
            {
                "visibility": "all",
                "affiliation": "owner,collaborator,organization_member",
                "sort": "full_name",
            }
        )
        return paged_list(token, f"{API_URL}/user/repos?{query}")

    query = urlencode({"type": "all", "sort": "full_name"})
    return paged_list(token, f"{API_URL}/users/{quote(login)}/repos?{query}")


def list_branches(token: str, full_name: str) -> list[dict]:
    owner, repo = full_name.split("/", 1)
    return paged_list(
        token,
        f"{API_URL}/repos/{quote(owner)}/{quote(repo)}/branches",
        allow_empty_repo=True,
    )


def list_authored_commits_on_branch(
    token: str, full_name: str, branch: str, login: str
) -> list[dict]:
    owner, repo = full_name.split("/", 1)
    query = urlencode({"sha": branch, "author": login})
    return paged_list(
        token,
        f"{API_URL}/repos/{quote(owner)}/{quote(repo)}/commits?{query}",
        allow_empty_repo=True,
    )


def scan_real_commits(
    token: str, login: str, include_private: bool
) -> tuple[dict[date, int], int, int]:
    """Conta commits reais alcançáveis por branches, deduplicando por SHA.

    Diferente de totalCommitContributions, isto inclui commits em branches que não
    são a default/gh-pages. Repositórios privados são lidos apenas no runner e seus
    nomes nunca entram no JSON/SVG público.
    """
    repositories = list_repositories(token, login, include_private)
    seen_shas: set[str] = set()
    daily: dict[date, int] = {}
    branches_scanned = 0
    now = datetime.now(UTC)

    for repository in repositories:
        full_name = str(repository.get("full_name", ""))
        if not full_name or repository.get("disabled"):
            continue

        branches = list_branches(token, full_name)
        for branch_info in branches:
            branch = str(branch_info.get("name", ""))
            if not branch:
                continue
            branches_scanned += 1

            commits = list_authored_commits_on_branch(token, full_name, branch, login)
            for item in commits:
                sha = str(item.get("sha", ""))
                if not sha or sha in seen_shas:
                    continue
                seen_shas.add(sha)

                commit = item.get("commit") or {}
                author = commit.get("author") or {}
                committer = commit.get("committer") or {}
                raw_date = author.get("date") or committer.get("date")
                if not raw_date:
                    continue

                try:
                    authored_at = datetime.fromisoformat(
                        str(raw_date).replace("Z", "+00:00")
                    ).astimezone(UTC)
                except ValueError:
                    continue

                if authored_at > now + timedelta(days=1):
                    continue
                day = authored_at.date()
                daily[day] = daily.get(day, 0) + 1

    return daily, len(repositories), branches_scanned


def fill_daily_series(daily_counts: dict[date, int]) -> list[dict[str, object]]:
    if not daily_counts:
        return []

    first_day = min(daily_counts)
    last_day = datetime.now(UTC).date()
    current = first_day
    running = 0
    series: list[dict[str, object]] = []

    while current <= last_day:
        count = daily_counts.get(current, 0)
        running += count
        series.append(
            {
                "date": current.isoformat(),
                "commits": count,
                "total": running,
            }
        )
        current += timedelta(days=1)

    return series


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
    private_note = (
        "repositórios privados permanecem anônimos"
        if include_private
        else "somente repositórios públicos acessíveis"
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="320" viewBox="0 0 900 320" role="img" aria-labelledby="title desc">
  <title id="title">Evolução dos commits de {login}</title>
  <desc id="desc">Gráfico acumulado dos commits reais alcançáveis por branches, de {first_date} até {last_date}. Total atual: {total}. {private_note}.</desc>
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
  <text x="42" y="38" class="title">EVOLUÇÃO DOS COMMITS</text>
  <text x="858" y="38" text-anchor="end" class="value">{format_pt(total)}</text>
  <text x="42" y="60" class="subtitle">todas as branches acessíveis · {private_note}</text>
  <text x="858" y="60" text-anchor="end" class="small">@{login}</text>

{grid}
  <line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#3A4550" stroke-width="1.2"/>
{line_markup}
{chr(10).join(x_labels)}
</svg>
'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Atualiza o gráfico real de commits do perfil.")
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

    daily_counts, repositories_scanned, branches_scanned = scan_real_commits(
        token, args.user, include_private
    )
    series = fill_daily_series(daily_counts)
    total = sum(daily_counts.values())

    payload = {
        "schema": 4,
        "user": args.user,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "scope": "all_accessible_repositories" if include_private else "public_repositories",
        "includes_private_commits": include_private,
        "private_repository_details_published": False,
        "counting_method": "unique authored commits reachable from accessible branches, deduplicated globally by SHA",
        "repositories_scanned": repositories_scanned,
        "branches_scanned": branches_scanned,
        "first_commit_date": series[0]["date"] if series else None,
        "total_commits": total,
        "series": series,
    }

    svg_path = Path(args.svg)
    data_path = Path(args.data)
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(render_svg(args.user, total, series, include_private), encoding="utf-8")
    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"Atualizado: {format_pt(total)} commits reais; "
        f"{repositories_scanned} repositórios e {branches_scanned} branches varridos."
    )


if __name__ == "__main__":
    main()
