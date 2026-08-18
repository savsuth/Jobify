import httpx
import respx

from src.integrations.github import extract_project_repos, fetch_github_context, fetch_repo_context

_SAMPLE_RESUME_LATEX = r"""
\begin{center}
    \textcolor{blue}{\href{https://github.com/savsuth}{GitHub}}
\end{center}

\section{Projects}
    \resumeProjectHeading
        {\textbf{TokenPress} $|$ \textcolor{blue}{{\href{https://github.com/savsuth/Tokenpress}{GitHub}}}}{}
    \resumeProjectHeading
        {\textbf{Turbovec} $|$ \textcolor{blue}{{\href{https://github.com/savsuth/Turbovec}{GitHub}}}}{}
"""


# --- extract_project_repos ------------------------------------------------------

def test_extract_project_repos_finds_project_links_with_names():
    repos = extract_project_repos(_SAMPLE_RESUME_LATEX)
    assert len(repos) == 2
    names = {r.name for r in repos}
    assert names == {"TokenPress", "Turbovec"}
    tokenpress = next(r for r in repos if r.name == "TokenPress")
    assert tokenpress.owner == "savsuth"
    assert tokenpress.repo == "Tokenpress"
    assert tokenpress.url == "https://github.com/savsuth/Tokenpress"


def test_extract_project_repos_excludes_bare_profile_link():
    # github.com/savsuth (no second path segment) is the header's profile link,
    # not a project repo - must never be treated as one.
    repos = extract_project_repos(_SAMPLE_RESUME_LATEX)
    assert all(r.repo != "savsuth" for r in repos)
    assert not any(r.owner == "savsuth" and r.name is None for r in repos)


def test_extract_project_repos_against_real_resume():
    raw_latex = open("profile/resume.tex").read()
    repos = extract_project_repos(raw_latex)
    assert {r.name for r in repos} == {
        "TokenPress",
        "Turbovec",
        "Traffic-Aware Routing System",
        "Derivatives Pricing Dashboard",
    }
    assert all(r.owner == "savsuth" for r in repos)


def test_extract_project_repos_deduplicates_repeated_links():
    latex = (
        r"\textbf{Foo} \href{https://github.com/acme/foo}{GitHub}"
        "\n"
        r"\href{https://github.com/acme/foo}{GitHub again}"
    )
    repos = extract_project_repos(latex)
    assert len(repos) == 1


def test_extract_project_repos_empty_for_no_links():
    assert extract_project_repos("no links here") == []


# --- fetch_repo_context -----------------------------------------------------------

@respx.mock
def test_fetch_repo_context_returns_real_fields():
    respx.get("https://api.github.com/repos/acme/foo").mock(
        return_value=httpx.Response(
            200,
            json={
                "description": "A foo library",
                "topics": ["python", "cli"],
                "stargazers_count": 42,
                "html_url": "https://github.com/acme/foo",
            },
        )
    )
    respx.get("https://api.github.com/repos/acme/foo/languages").mock(
        return_value=httpx.Response(200, json={"Python": 12345, "Shell": 100})
    )
    import base64

    encoded_readme = base64.b64encode(b"# Foo\nA useful tool.").decode()
    respx.get("https://api.github.com/repos/acme/foo/readme").mock(
        return_value=httpx.Response(200, json={"content": encoded_readme})
    )

    with httpx.Client() as client:
        ctx = fetch_repo_context("acme", "foo", client)

    assert ctx is not None
    assert ctx.description == "A foo library"
    assert ctx.languages == {"Python": 12345, "Shell": 100}
    assert ctx.topics == ["python", "cli"]
    assert ctx.stars == 42
    assert "A useful tool." in ctx.readme_excerpt


@respx.mock
def test_fetch_repo_context_returns_none_on_404():
    respx.get("https://api.github.com/repos/acme/missing").mock(return_value=httpx.Response(404))

    with httpx.Client() as client:
        ctx = fetch_repo_context("acme", "missing", client)

    assert ctx is None


@respx.mock
def test_fetch_repo_context_survives_missing_languages_and_readme():
    respx.get("https://api.github.com/repos/acme/bare").mock(
        return_value=httpx.Response(200, json={"description": None, "html_url": "https://github.com/acme/bare"})
    )
    respx.get("https://api.github.com/repos/acme/bare/languages").mock(return_value=httpx.Response(404))
    respx.get("https://api.github.com/repos/acme/bare/readme").mock(return_value=httpx.Response(404))

    with httpx.Client() as client:
        ctx = fetch_repo_context("acme", "bare", client)

    assert ctx is not None
    assert ctx.languages == {}
    assert ctx.readme_excerpt is None
    assert ctx.stars == 0


# --- fetch_github_context ----------------------------------------------------------

@respx.mock
def test_fetch_github_context_aggregates_and_skips_unavailable_repos():
    respx.get("https://api.github.com/repos/savsuth/Tokenpress").mock(
        return_value=httpx.Response(
            200, json={"description": "Compresses tokens", "html_url": "https://github.com/savsuth/Tokenpress"}
        )
    )
    respx.get("https://api.github.com/repos/savsuth/Tokenpress/languages").mock(
        return_value=httpx.Response(200, json={"Python": 5000})
    )
    respx.get("https://api.github.com/repos/savsuth/Tokenpress/readme").mock(return_value=httpx.Response(404))

    respx.get("https://api.github.com/repos/savsuth/Turbovec").mock(return_value=httpx.Response(404))
    respx.get("https://api.github.com/repos/savsuth/Turbovec/languages").mock(return_value=httpx.Response(404))
    respx.get("https://api.github.com/repos/savsuth/Turbovec/readme").mock(return_value=httpx.Response(404))

    contexts = fetch_github_context(_SAMPLE_RESUME_LATEX)

    assert len(contexts) == 1
    assert contexts[0]["project_name"] == "TokenPress"
    assert contexts[0]["description"] == "Compresses tokens"
    assert contexts[0]["languages"] == {"Python": 5000}


def test_fetch_github_context_returns_empty_list_for_no_repos():
    assert fetch_github_context("no github links at all") == []


@respx.mock
def test_fetch_github_context_reuses_a_single_client_not_refetching():
    # One call per endpoint per repo - confirms no duplicate/refetch behavior.
    repo_route = respx.get("https://api.github.com/repos/savsuth/Tokenpress").mock(
        return_value=httpx.Response(200, json={"html_url": "https://github.com/savsuth/Tokenpress"})
    )
    respx.get("https://api.github.com/repos/savsuth/Tokenpress/languages").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://api.github.com/repos/savsuth/Tokenpress/readme").mock(return_value=httpx.Response(404))

    single_project_latex = r"\textbf{TokenPress} \href{https://github.com/savsuth/Tokenpress}{GitHub}"
    fetch_github_context(single_project_latex)

    assert repo_route.call_count == 1


# --- optional GITHUB_TOKEN auth header ---------------------------------------------

@respx.mock
def test_fetch_repo_context_sends_auth_header_when_token_configured(monkeypatch):
    from src.config import get_settings

    monkeypatch.setenv("GITHUB_TOKEN", "test-token-123")
    get_settings.cache_clear()

    route = respx.get("https://api.github.com/repos/acme/foo").mock(
        return_value=httpx.Response(200, json={"html_url": "https://github.com/acme/foo"})
    )
    respx.get("https://api.github.com/repos/acme/foo/languages").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://api.github.com/repos/acme/foo/readme").mock(return_value=httpx.Response(404))

    with httpx.Client() as client:
        fetch_repo_context("acme", "foo", client)

    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token-123"
    get_settings.cache_clear()


@respx.mock
def test_fetch_repo_context_omits_auth_header_when_no_token():
    route = respx.get("https://api.github.com/repos/acme/foo").mock(
        return_value=httpx.Response(200, json={"html_url": "https://github.com/acme/foo"})
    )
    respx.get("https://api.github.com/repos/acme/foo/languages").mock(return_value=httpx.Response(200, json={}))
    respx.get("https://api.github.com/repos/acme/foo/readme").mock(return_value=httpx.Response(404))

    with httpx.Client() as client:
        fetch_repo_context("acme", "foo", client)

    assert "Authorization" not in route.calls[0].request.headers
