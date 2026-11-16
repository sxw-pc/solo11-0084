"""Deterministic HTML rendering for the synthetic local portal."""

from __future__ import annotations

from html import escape

from .fixture import DatasetFixture, PortalFixture, VersionFixture
from .util import canonical_json

_STYLE = """
body{font-family:system-ui,sans-serif;max-width:980px;margin:2rem auto;padding:0 1rem;color:#17211b;background:#f7f8f3}
header,footer{border-bottom:1px solid #aab7ad;padding:.6rem 0}footer{border-top:1px solid #aab7ad;border-bottom:0;margin-top:2rem}
h1,h2,h3{color:#204b34}.muted{color:#52645a}.card{border:1px solid #b8c6bc;border-radius:8px;padding:1rem;margin:1rem 0;background:#fff}
code{background:#edf1ea;padding:.1rem .25rem;border-radius:3px;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #d5ddd6;padding:.35rem;text-align:left}
""".strip()


def _layout(title: str, body: str, portal: PortalFixture) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>{escape(title)}</title>'
        f"<style>{_STYLE}</style></head><body>"
        f"<header><strong>{escape(portal.portal_title)}</strong> "
        f'<span class="muted">synthetic fixture portal</span></header>'
        f"{body}"
        f"<footer>{escape(portal.publisher)} - offline demonstration data only.</footer>"
        "</body></html>\n"
    )


def render_home(portal: PortalFixture) -> str:
    cards = []
    for dataset in portal.datasets:
        version_count = len(dataset.versions)
        artifact_count = sum(len(version.artifacts) for version in dataset.versions)
        cards.append(
            '<article class="card">'
            f'<h3><a class="dataset-link" href="/datasets/{escape(dataset.dataset_id)}" '
            f'data-dataset-id="{escape(dataset.dataset_id)}">{escape(dataset.title)}</a></h3>'
            f"<p>{escape(dataset.summary)}</p>"
            f'<p class="muted">{escape(dataset.publisher)} | {escape(dataset.license)} | '
            f"{version_count} version(s) | {artifact_count} release file(s)</p>"
            "</article>"
        )
    body = (
        f'<main data-page="home"><h1>{escape(portal.portal_title)}</h1>'
        f"<p>{escape(portal.portal_description)}</p>"
        "<h2>Published datasets</h2>" + "".join(cards) + "</main>"
    )
    return _layout(portal.portal_title, body, portal)


def render_dataset(dataset: DatasetFixture, portal: PortalFixture) -> str:
    rows = []
    for version in dataset.versions:
        rows.append(
            "<tr>"
            f'<td><a class="version-link" href="/datasets/{escape(dataset.dataset_id)}'
            f'/versions/{escape(version.version_id)}" data-version-id="{escape(version.version_id)}" '
            f'data-channel="{escape(version.channel)}" data-released-at="{escape(version.released_at)}">'
            f"{escape(version.label)}</a></td>"
            f"<td>{escape(version.channel)}</td><td>{escape(version.released_at)}</td>"
            f"<td>{len(version.artifacts)}</td></tr>"
        )
    body = (
        f'<main data-page="dataset" data-dataset-id="{escape(dataset.dataset_id)}" '
        f'data-title="{escape(dataset.title)}" data-publisher="{escape(dataset.publisher)}" '
        f'data-license="{escape(dataset.license)}">'
        f'<p><a href="/">All datasets</a></p><h1>{escape(dataset.title)}</h1>'
        f"<p>{escape(dataset.summary)}</p>"
        f'<p class="muted">Publisher: {escape(dataset.publisher)} | License: {escape(dataset.license)}</p>'
        "<h2>Dataset versions</h2><table><thead><tr><th>Version</th><th>Channel</th>"
        "<th>Released</th><th>Files</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></main>"
    )
    return _layout(dataset.title, body, portal)


def render_version(
    dataset: DatasetFixture, version: VersionFixture, portal: PortalFixture
) -> str:
    rows = []
    for artifact in version.artifacts:
        href = f"/files/{dataset.dataset_id}/{version.version_id}/{artifact.name}"
        schema_attr = (
            f' data-schema="{escape(canonical_json(artifact.schema.to_dict()), quote=True)}"'
            if artifact.schema is not None
            else ""
        )
        rows.append(
            '<tr><td><a class="artifact-link" href="'
            + escape(href)
            + f'" data-filename="{escape(artifact.name)}" '
            + f'data-size="{artifact.declared_size}" '
            + f'data-sha256="{escape(artifact.declared_sha256)}" '
            + f'data-media-type="{escape(artifact.media_type)}"{schema_attr}>'
            + f"{escape(artifact.name)}</a></td>"
            + f"<td>{escape(artifact.media_type)}</td><td>{artifact.declared_size}</td>"
            + f"<td><code>{escape(artifact.declared_sha256)}</code></td></tr>"
        )
    body = (
        f'<main data-page="version" data-dataset-id="{escape(dataset.dataset_id)}" '
        f'data-version-id="{escape(version.version_id)}" '
        f'data-version-label="{escape(version.label)}" data-channel="{escape(version.channel)}" '
        f'data-released-at="{escape(version.released_at)}">'
        f'<p><a href="/datasets/{escape(dataset.dataset_id)}">Back to dataset</a></p>'
        f"<h1>{escape(dataset.title)} / {escape(version.label)}</h1>"
        f'<p class="muted">Channel: {escape(version.channel)} | Released: '
        f"{escape(version.released_at)}</p><p>{escape(version.notes)}</p>"
        "<h2>Release artifacts</h2><table><thead><tr><th>File</th><th>Media type</th>"
        "<th>Declared bytes</th><th>Declared SHA-256</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></main>"
    )
    return _layout(f"{dataset.title} {version.version_id}", body, portal)
