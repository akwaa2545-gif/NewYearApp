"""Generate a private test CA and LAN HTTPS certificate for EventDesk."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import ipaddress
from pathlib import Path
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def detect_lan_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 80))
        return probe.getsockname()[0]
    finally:
        probe.close()


def write_private_key(path, key):
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))


def generate(lan_ips):
    addresses = list(dict.fromkeys(ipaddress.ip_address(value) for value in lan_ips))
    if not addresses or any(not address.is_private for address in addresses):
        raise ValueError("Use this only with private LAN IP addresses.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "EventDesk Test CA")])
    ca_cert = (x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
            key_cert_sign=True, key_agreement=False, content_commitment=False,
            data_encipherment=False, crl_sign=True, encipher_only=False,
            decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256()))

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(addresses[0]))])
    server_cert = (x509.CertificateBuilder()
        .subject_name(server_name).issuer_name(ca_name).public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName([
            *(x509.IPAddress(address) for address in addresses),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True,
            key_cert_sign=False, key_agreement=False, content_commitment=False,
            data_encipherment=False, crl_sign=False, encipher_only=False,
            decipher_only=False), critical=True)
        .sign(ca_key, hashes.SHA256()))

    ca_pem = DATA_DIR / "eventdesk-test-ca.pem"
    ca_der = DATA_DIR / "eventdesk-test-ca.cer"
    server_pem = DATA_DIR / "lan-server-cert.pem"
    server_key_path = DATA_DIR / "lan-server-key.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    ca_der.write_bytes(ca_cert.public_bytes(serialization.Encoding.DER))
    server_pem.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    write_private_key(server_key_path, server_key)
    return ca_der, server_pem, server_key_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", action="append", dest="ips",
                        help="Private LAN IP address; repeat for additional interfaces")
    args = parser.parse_args()
    ips = args.ips or [detect_lan_ip()]
    ca_path, cert_path, key_path = generate(ips)
    print("HTTPS certificate created for: " + ", ".join(
        f"https://{address}:8080" for address in ips))
    print(f"Install this CA certificate on each test device: {ca_path}")
    print(f"Server certificate: {cert_path}")
    print(f"Private server key: {key_path} (do not share)")


if __name__ == "__main__":
    main()
