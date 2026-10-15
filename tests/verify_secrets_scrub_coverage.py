"""#489 - every credential the process holds is scrubbed from Prompt Tap and Databricks text.

`secrets_scrub._live_secrets()` (exact-VALUE replacement used by `scrub` / `scrub_secrets`) and
`known_keys` (the engine key-echo guard) must read ONE shared list of config credential names,
so a credential added to the list is covered on both paths. Typesafe (no prefix shape) and
the Databricks token / client secret were missing from the scrub path. Offline, ASCII-only.
"""

import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_ROOT)
sys.path.insert(0, _ROOT)
from tests._bootstrap import init_env

init_env("loctms_scrubcov_")

from app import config
from app.services import prompt_tap, secrets_scrub
from app.services.secrets_scrub import REDACTION, known_keys, scrub, scrub_secrets

# Prefix-less fixture values: only an exact-value match can remove them.
FIXTURES = (
    ("ANTHROPIC_API_KEY", "QZXanthropicFixtureValue000111"),
    ("GEMINI_API_KEY", "QZXgeminiFixtureValue00011122"),
    ("OPENAI_API_KEY", "QZXopenaiFixtureValue00011122"),
    ("TYPESAFE_API_KEY", "QZXtypesafeFixtureValue0001112"),
    ("LOCAL_API_KEY", "QZXlocalFixtureValue0001112223"),
    ("S3_SECRET_ACCESS_KEY", "QZXs3SecretFixtureValue00011122"),
    ("S3_SESSION_TOKEN", "QZXs3SessionFixtureValue0001112"),
    ("DATABRICKS_TOKEN", "QZXdatabricksTokenFixture0001112"),
    ("DATABRICKS_CLIENT_SECRET", "QZXdatabricksClientFixture00011"),
    ("SESSION_SECRET", "QZXsessionSecretFixtureValue0001"),
    ("ENCRYPTION_KEY", "QZXencryptionKeyFixtureValue0001"),
)

SHORT_VALUE = "short"


def ok(cond, msg):
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)


def main():
    saved = {name: getattr(config, name, None) for name, _ in FIXTURES}
    try:
        for name, value in FIXTURES:
            setattr(config, name, value)
        live = secrets_scrub._live_secrets()
        for name, value in FIXTURES:
            ok(value in live, f"{name} is missing from _live_secrets()")
            text = f"upstream said: {value} was rejected"
            got, changed = scrub(text, error_channel=False)
            ok(changed and value not in got and REDACTION in got, f"{name} not scrubbed: {got}")
            ok(value not in scrub_secrets(text), f"{name} survives scrub_secrets")
            got_tap, hit = prompt_tap._scrub(text, error_channel=False)
            ok(hit and value not in got_tap, f"{name} survives the Prompt Tap scrub")
        # one shared list: every echo-guard key is also an exact-value scrub key
        guard = set(known_keys())
        ok(guard <= set(live), f"known_keys has values the scrubber lacks: {guard - set(live)}")
        # the shared name list is the single source for both consumers
        ok(
            set(secrets_scrub.CREDENTIAL_CONFIG_NAMES) >= {n for n, _ in FIXTURES},
            "CREDENTIAL_CONFIG_NAMES must name every config credential",
        )
        # discovery: a NEW credential-looking config attribute must be added to the shared list
        # (or to the reviewed non-secret list below), so the next one cannot be forgotten.
        non_secret = {
            "MAX_CHUNK_TOKENS",
            "PROVIDER_TOKENS_PER_SEC",
            "SESSION_SECRET_MIN_LENGTH",
            "S3_ACCESS_KEY_ID",  # an identifier, not a secret
            "SESSION_SECRET_SOURCE",  # a label (env/keyfile), not the secret
            "ENCRYPTION_KEY_SOURCE",  # a label (env/keyfile), not the secret
        }
        # Gate of #489: `_KEY` is a marker too (ENCRYPTION_KEY carries none of
        # the others and was missed). KEY_IMAGE_* size limits are excluded below.
        marker = re.compile(r"(SECRET|TOKEN|API_KEY|_KEY$|_KEY_|PASSWORD|PASSPHRASE)")
        found = {
            n
            for n in dir(config)
            if n.isupper()
            and not n.startswith("_")
            and marker.search(n)
            and not n.startswith("KEY_IMAGE")
        }
        missing = found - set(secrets_scrub.CREDENTIAL_CONFIG_NAMES) - non_secret
        ok(not missing, f"config credential(s) not in CREDENTIAL_CONFIG_NAMES: {sorted(missing)}")
        # a short or unset value never garbles text
        config.DATABRICKS_TOKEN = SHORT_VALUE
        ok(scrub_secrets("a short word") == "a short word", "short value must not be scrubbed")
    finally:
        for name, value in saved.items():
            setattr(config, name, value)
    print("SECRETS SCRUB COVERAGE OK")


if __name__ == "__main__":
    main()
