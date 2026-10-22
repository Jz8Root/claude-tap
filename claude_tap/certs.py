"""CA and per-host certificate generation for forward proxy TLS termination.

Generates a self-signed CA on first run and creates per-host certificates
signed by that CA. The CA cert/key are persisted to disk so they survive
restarts; host certs are cached in memory for the lifetime of the process.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import shutil
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

log = logging.getLogger("claude-tap")

# Default directory for CA files
_DEFAULT_CA_DIR = Path.home() / ".claude-tap"

# CA validity: 5 years
_CA_VALIDITY_DAYS = 5 * 365
# Host cert validity: 1 year
_HOST_VALIDITY_DAYS = 365


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ensure_ca(ca_dir: Path | None = None) -> tuple[Path, Path]:
    """Ensure a CA certificate and key exist on disk.

    Returns (ca_cert_path, ca_key_path). Creates them if they don't exist.
    """
    ca_dir = ca_dir or _DEFAULT_CA_DIR
    ca_dir.mkdir(parents=True, exist_ok=True)

    ca_cert_path = ca_dir / "ca.pem"
    ca_key_path = ca_dir / "ca-key.pem"

    if ca_cert_path.exists() and ca_key_path.exists():
        # Validate existing files are loadable
        try:
            _load_ca(ca_cert_path, ca_key_path)
            return ca_cert_path, ca_key_path
        except Exception:
            log.warning("Existing CA files are invalid, regenerating")

    log.info(f"Generating new CA certificate in {ca_dir}")
    key = _generate_key()
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "claude-tap CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "claude-tap"),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    ca_key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Restrict key file permissions
    ca_key_path.chmod(0o600)

    ca_cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    log.info(f"CA certificate written to {ca_cert_path}")
    return ca_cert_path, ca_key_path


def macos_login_keychain_path() -> Path:
    """Return the current user's login keychain path on modern macOS."""
    return Path.home() / "Library" / "Keychains" / "login.keychain-db"


def build_macos_verify_ca_command(ca_cert_path: Path, keychain_path: Path | None = None) -> list[str]:
    """Build a non-mutating command that checks whether the CA is trusted for TLS."""
    keychain = keychain_path or macos_login_keychain_path()
    return [
        "security",
        "verify-cert",
        "-c",
        str(ca_cert_path),
        "-p",
        "ssl",
        "-l",
        "-L",
        "-q",
        "-k",
        str(keychain),
    ]


def build_macos_trust_ca_command(ca_cert_path: Path, keychain_path: Path | None = None) -> list[str]:
    """Build the no-sudo command that trusts the CA in the current user's keychain."""
    keychain = keychain_path or macos_login_keychain_path()
    return [
        "security",
        "add-trusted-cert",
        "-r",
        "trustRoot",
        "-p",
        "ssl",
        "-k",
        str(keychain),
        str(ca_cert_path),
    ]


def is_macos_ca_trusted(ca_cert_path: Path, keychain_path: Path | None = None) -> bool:
    """Return True when macOS already trusts the CA for TLS in the user keychain."""
    result = subprocess.run(
        build_macos_verify_ca_command(ca_cert_path, keychain_path),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def trust_macos_ca(ca_cert_path: Path, keychain_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Trust the CA for TLS in the current user's macOS login keychain.

    This intentionally does not use ``sudo`` or the System keychain. macOS may
    still prompt for the user's login-keychain password.
    """
    return subprocess.run(
        build_macos_trust_ca_command(ca_cert_path, keychain_path),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Linux system CA trust
# ---------------------------------------------------------------------------


def _linux_ca_bundle_dirs() -> list[Path]:
    """Return candidate system CA extra-certs directories, most-specific first."""
    return [
        Path("/usr/local/share/ca-certificates"),  # Debian/Ubuntu
        Path("/etc/pki/ca-trust/source/anchors"),  # Fedora/RHEL/CentOS
        Path("/etc/ca-certificates/trust-source/anchors"),  # Arch
        Path("/usr/share/pki/trust/anchors"),  # openSUSE
    ]


def linux_ca_extra_dir() -> Path | None:
    """Return the first writable system CA extra-certs directory, or None."""
    for d in _linux_ca_bundle_dirs():
        if d.exists():
            return d
    return None


def linux_update_ca_command() -> str | None:
    """Return the distro's update-ca command, or None if not found."""
    for cmd in ("update-ca-certificates", "update-ca-trust", "trust"):
        if shutil.which(cmd):
            return cmd
    return None


def is_linux_ca_trusted(ca_cert_path: Path) -> bool:
    """Return True when the CA cert is already present in the system extra-certs dir."""
    extra_dir = linux_ca_extra_dir()
    if extra_dir is None:
        return False
    dest = extra_dir / "claude-tap.crt"
    if not dest.exists():
        return False
    try:
        return dest.read_bytes() == ca_cert_path.read_bytes()
    except OSError:
        return False


def trust_linux_ca(ca_cert_path: Path) -> tuple[bool, str]:
    """Install the CA into the system store and run update-ca-certificates.

    Returns (success, message). Requires write access to the CA extra-certs dir
    (typically needs sudo); the caller is responsible for privilege escalation.
    """
    extra_dir = linux_ca_extra_dir()
    if extra_dir is None:
        return False, "No supported system CA extra-certs directory found."

    dest = extra_dir / "claude-tap.crt"
    try:
        dest.write_bytes(ca_cert_path.read_bytes())
    except PermissionError:
        return False, f"Permission denied writing to {dest} — re-run with sudo."

    update_cmd = linux_update_ca_command()
    if update_cmd is None:
        return False, f"CA copied to {dest} but no update-ca command found (install ca-certificates)."

    result = subprocess.run([update_cmd], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        return False, f"{update_cmd} failed: {details}"

    return True, str(dest)


# ---------------------------------------------------------------------------
# Firefox NSS trust (certutil)
# ---------------------------------------------------------------------------


def _firefox_nss_profile_dirs() -> list[Path]:
    """Return existing Firefox NSS profile directories for the current user."""
    candidates: list[Path] = []
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:
        base = home / ".mozilla" / "firefox"

    if base.is_dir():
        for p in base.iterdir():
            if p.is_dir() and (p / "cert9.db").exists():
                candidates.append(p)
            elif p.is_dir() and (p / "cert8.db").exists():
                candidates.append(p)

    # Snap Firefox
    snap_base = home / "snap" / "firefox" / "current" / ".mozilla" / "firefox"
    if snap_base.is_dir():
        for p in snap_base.iterdir():
            if p.is_dir() and ((p / "cert9.db").exists() or (p / "cert8.db").exists()):
                candidates.append(p)

    return candidates


def _chrome_nss_profile_dirs() -> list[Path]:
    """Return existing Chromium/Chrome NSS profile directories (Linux only)."""
    candidates: list[Path] = []
    home = Path.home()
    bases = [
        home / ".pki" / "nssdb",  # Chromium shared NSS on Linux
        home / ".config" / "google-chrome",  # Chrome
        home / ".config" / "chromium",  # Chromium
        home / "snap" / "chromium" / "current" / ".pki" / "nssdb",
    ]
    for b in bases:
        if b.is_dir():
            candidates.append(b)
    return candidates


def _certutil_bin() -> str | None:
    return shutil.which("certutil")


def is_nss_ca_trusted(nss_dir: Path) -> bool:
    """Return True when the claude-tap CA is trusted in the given NSS database."""
    certutil = _certutil_bin()
    if certutil is None:
        return False
    result = subprocess.run(
        [certutil, "-L", "-d", f"sql:{nss_dir}", "-n", "claude-tap"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def trust_nss_ca(ca_cert_path: Path, nss_dir: Path, nickname: str = "claude-tap") -> tuple[bool, str]:
    """Add the CA to the given NSS database with trust bits CT,,."""
    certutil = _certutil_bin()
    if certutil is None:
        return False, "certutil not found — install libnss3-tools (Debian/Ubuntu) or nss-tools (Fedora)."

    # Remove any existing entry first (ignore failure — may not exist)
    subprocess.run(
        [certutil, "-D", "-d", f"sql:{nss_dir}", "-n", nickname],
        capture_output=True,
        check=False,
    )

    result = subprocess.run(
        [certutil, "-A", "-d", f"sql:{nss_dir}", "-n", nickname, "-t", "CT,,", "-i", str(ca_cert_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "").strip()
        return False, f"certutil failed for {nss_dir}: {details}"

    return True, str(nss_dir)


def trust_firefox_ca(ca_cert_path: Path) -> list[tuple[bool, str]]:
    """Trust CA in all detected Firefox NSS profiles. Returns one (ok, msg) per profile."""
    profiles = _firefox_nss_profile_dirs()
    if not profiles:
        return [(False, "No Firefox NSS profiles found.")]
    return [trust_nss_ca(ca_cert_path, p) for p in profiles]


def trust_chrome_nss_ca(ca_cert_path: Path) -> list[tuple[bool, str]]:
    """Trust CA in Chrome/Chromium NSS stores (Linux). Returns one (ok, msg) per store."""
    dirs = _chrome_nss_profile_dirs()
    if not dirs:
        return [(False, "No Chrome/Chromium NSS databases found.")]
    return [trust_nss_ca(ca_cert_path, d) for d in dirs]


# ---------------------------------------------------------------------------


def _load_ca(ca_cert_path: Path, ca_key_path: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Load CA cert and key from PEM files."""
    ca_cert = x509.load_pem_x509_certificate(ca_cert_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    return ca_cert, ca_key  # type: ignore[return-value]


class CertificateAuthority:
    """In-memory CA that generates per-host TLS certificates.

    Caches generated host certs for the lifetime of the process.
    """

    def __init__(self, ca_cert_path: Path, ca_key_path: Path) -> None:
        self._ca_cert, self._ca_key = _load_ca(ca_cert_path, ca_key_path)
        self._host_cache: dict[str, tuple[bytes, bytes]] = {}

    def get_host_cert_pem(self, hostname: str) -> tuple[bytes, bytes]:
        """Return (cert_pem, key_pem) for the given hostname.

        Generates and caches a new certificate signed by the CA if needed.
        """
        if hostname in self._host_cache:
            return self._host_cache[hostname]

        key = _generate_key()
        now = datetime.datetime.now(datetime.timezone.utc)

        subject = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, hostname),
            ]
        )

        # Build SAN: use IPAddress for IP addresses, DNSName for hostnames
        san_names: list[x509.GeneralName] = []
        try:
            ip = ipaddress.ip_address(hostname)
            san_names.append(x509.IPAddress(ip))
        except ValueError:
            san_names.append(x509.DNSName(hostname))

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=_HOST_VALIDITY_DAYS))
            .add_extension(
                x509.SubjectAlternativeName(san_names),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
        )

        cert = builder.sign(self._ca_key, hashes.SHA256())

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        key_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )

        self._host_cache[hostname] = (cert_pem, key_pem)
        return cert_pem, key_pem

    def make_ssl_context(self, hostname: str) -> ssl.SSLContext:
        """Create an SSL context for serving TLS as the given hostname."""

        cert_pem, key_pem = self.get_host_cert_pem(hostname)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Write cert and key to temp files (ssl module needs file paths)
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem)
            cert_path = cf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem)
            key_path = kf.name

        try:
            ctx.load_cert_chain(cert_path, key_path)
        finally:
            Path(cert_path).unlink(missing_ok=True)
            Path(key_path).unlink(missing_ok=True)

        return ctx
