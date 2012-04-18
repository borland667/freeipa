# Authors: Rob Crittenden <rcritten@redhat.com>
#          Ade Lee <alee@redhat.com>
#          Andrew Wnuk <awnuk@redhat.com>
#
# Copyright (C) 2009  Red Hat
# see file 'COPYING' for use and warranty information
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import pwd
import os
import sys
import re
import time
import ldap
import base64
import array
import tempfile
import binascii
import shutil
import httplib
import urllib
import xml.dom.minidom
import stat
import socket
from ipapython import dogtag
from ipapython.certdb import get_ca_nickname
from ipalib import pkcs10, x509
from ipalib.dn import DN
import subprocess

from nss.error import NSPRError
import nss.nss as nss

from ipapython import ipautil
from ipapython import nsslib
from ipapython import services as ipaservices

from ipaserver import ipaldap
from ipaserver.install import service
from ipaserver.install import installutils
from ipaserver.install import dsinstance
from ipaserver.install import certs
from ipaserver.install.installutils import ReplicaConfig
from ipalib import util
from ipapython.ipa_log_manager import *

HTTPD_CONFD = "/etc/httpd/conf.d/"
DEFAULT_DSPORT=7389

PKI_USER = "pkiuser"
PKI_DS_USER = "pkisrv"

# These values come from /usr/share/pki/ca/setup/postinstall
PKI_INSTANCE_NAME="pki-ca"
AGENT_SECURE_PORT=9443
EE_SECURE_PORT=9444
ADMIN_SECURE_PORT=9445
EE_CLIENT_AUTH_PORT=9446
UNSECURE_PORT=9180
TOMCAT_SERVER_PORT=9701

IPA_SERVICE_PROFILE = '/var/lib/%s/profiles/ca/caIPAserviceCert.cfg' % PKI_INSTANCE_NAME

# We need to reset the template because the CA uses the regular boot
# information
INF_TEMPLATE = """
[General]
FullMachineName=   $FQDN
SuiteSpotUserID=   $USER
SuiteSpotGroup=    $GROUP
ServerRoot=    $SERVER_ROOT
[slapd]
ServerPort=   $DSPORT
ServerIdentifier=   $SERVERID
Suffix=   $SUFFIX
RootDN=   cn=Directory Manager
RootDNPwd= $PASSWORD
"""

def check_inst():
    """
    Validate that the appropriate dogtag/RHCS packages have been installed.
    """

    # Check for a couple of binaries we need
    if not os.path.exists('/usr/bin/pkicreate'):
        return False
    if not os.path.exists('/usr/bin/pkisilent'):
        return False

    # This is the template tomcat file for a CA
    if not os.path.exists('/usr/share/pki/ca/conf/server.xml'):
        return False

    return True

def get_preop_pin(instance_root, instance_name):
    preop_pin = None

    filename = instance_root + "/" + instance_name + "/conf/CS.cfg"

    # read the config file and get the preop pin
    try:
        f=open(filename)
    except IOError, e:
        root_logger.error("Cannot open configuration file." + str(e))
        raise e
    data = f.read()
    data = data.split('\n')
    pattern = re.compile("preop.pin=(.*)" )
    for line in data:
        match = re.search(pattern, line)
        if (match):
            preop_pin=match.group(1)
            break

    if preop_pin is None:
        raise RuntimeError("Unable to find preop.pin in %s. Is your CA already configured?" % filename)

    return preop_pin

def import_pkcs12(input_file, input_passwd, cert_database,
                  cert_passwd):
    ipautil.run(["/usr/bin/pk12util", "-d", cert_database,
                 "-i", input_file,
                 "-k", cert_passwd,
                 "-w", input_passwd])

def get_value(s):
    """
    Parse out a name/value pair from a Javascript variable.
    """
    try:
        expr = s.split('=',1)
        value = expr[1]
        value = value.replace('\"', '')
        value = value.replace(';','')
        value = value.replace('\\n','\n')
        value = value.replace('\\r','\r')
        return value
    except IndexError:
        return None

def find_substring(data, value):
    """
    Scan through a list looking for a string that starts with value.
    """
    for d in data:
        if d.startswith(value):
            return get_value(d)

def get_defList(data):
    """
    Return a dictionary of defList name/value pairs.

    A certificate signing request is specified as a series of these.
    """
    varname = None
    value = None
    skip = False
    defdict = {}
    for d in data:
        if d.startswith("defList = new Object"):
            varname = None
            value = None
            skip = False
        if d.startswith("defList.defId"):
            varname = get_value(d)
        if d.startswith("defList.defVal"):
            value = get_value(d)
            if skip:
                varname = None
                value = None
                skip = False
        if d.startswith("defList.defConstraint"):
            ctype = get_value(d)
            if ctype == "readonly":
                skip = True

        if varname and value:
            defdict[varname] = value
            varname = None
            value = None

    return defdict

def get_outputList(data):
    """
    Return a dictionary of outputList name/value pairs.

    The output from issuing a certificate is a series of these.
    """
    varname = None
    value = None
    outputdict = {}
    for d in data:
        if d.startswith("outputList = new"):
            varname = None
            value = None
        if d.startswith("outputList.outputId"):
            varname = get_value(d)
        if d.startswith("outputList.outputVal"):
            value = get_value(d)

        if varname and value:
            outputdict[varname] = value
            varname = None
            value = None

    return outputdict

class CADSInstance(service.Service):
    def __init__(self, host_name=None, realm_name=None, domain_name=None, dm_password=None):
        service.Service.__init__(self, "pkids")
        self.serverid = "PKI-IPA"
        self.realm_name = realm_name
        self.dm_password = dm_password
        self.sub_dict = None
        self.domain = domain_name
        self.fqdn = host_name
        self.dercert = None
        self.pkcs12_info = None
        self.ds_port = None
        self.master_host = None
        self.nickname = 'Server-Cert'
        self.subject_base = None
        if host_name and realm_name:
            self.principal = "dogtagldap/%s@%s" % (self.fqdn, self.realm_name)
        if realm_name:
            self.suffix = ipautil.realm_to_suffix(self.realm_name)
            self.__setup_sub_dict()
        else:
            self.suffix = None

    def create_instance(self, realm_name, host_name, domain_name,
                        dm_password, pkcs12_info=None, ds_port=DEFAULT_DSPORT,
                        subject_base=None):
        self.ds_port = ds_port
        self.realm_name = realm_name.upper()
        self.suffix = ipautil.realm_to_suffix(self.realm_name)
        self.fqdn = host_name
        self.dm_password = dm_password
        self.domain = domain_name
        self.pkcs12_info = pkcs12_info
        self.subject_base = subject_base
        self.principal = "dogtagldap/%s@%s" % (self.fqdn, self.realm_name)
        self.__setup_sub_dict()

        self.step("creating directory server user", self.__create_ds_user)
        self.step("creating directory server instance", self.__create_instance)
        self.step("restarting directory server", self.restart_instance)

        self.start_creation("Configuring directory server for the CA", 30)

    def __setup_sub_dict(self):
        server_root = dsinstance.find_server_root()
        self.sub_dict = dict(FQDN=self.fqdn, SERVERID=self.serverid,
                             PASSWORD=self.dm_password, SUFFIX=self.suffix.lower(),
                             REALM=self.realm_name, USER=PKI_DS_USER,
                             SERVER_ROOT=server_root, DOMAIN=self.domain,
                             TIME=int(time.time()), DSPORT=self.ds_port,
                             GROUP=dsinstance.DS_GROUP)

    def __create_ds_user(self):
        try:
            pwd.getpwnam(PKI_DS_USER)
            root_logger.debug("ds user %s exists" % PKI_DS_USER)
        except KeyError:
            root_logger.debug("adding ds user %s" % PKI_DS_USER)
            args = ["/usr/sbin/useradd", "-g", dsinstance.DS_GROUP,
                                         "-c", "PKI DS System User",
                                         "-d", "/var/lib/dirsrv",
                                         "-s", "/sbin/nologin",
                                         "-M", "-r", PKI_DS_USER]
            try:
                ipautil.run(args)
                root_logger.debug("done adding user")
            except ipautil.CalledProcessError, e:
                root_logger.critical("failed to add user %s" % e)

    def __create_instance(self):
        self.backup_state("serverid", self.serverid)

        inf_txt = ipautil.template_str(INF_TEMPLATE, self.sub_dict)
        root_logger.debug("writing inf template")
        inf_fd = ipautil.write_tmp_file(inf_txt)
        inf_txt = re.sub(r"RootDNPwd=.*\n", "", inf_txt)
        root_logger.debug(inf_txt)
        if ipautil.file_exists("/usr/sbin/setup-ds.pl"):
            args = ["/usr/sbin/setup-ds.pl", "--silent", "--logfile", "-", "-f", inf_fd.name]
            root_logger.debug("calling setup-ds.pl")
        else:
            args = ["/usr/bin/ds_newinst.pl", inf_fd.name]
            root_logger.debug("calling ds_newinst.pl")
        try:
            ipautil.run(args)
            root_logger.debug("completed creating ds instance")
        except ipautil.CalledProcessError, e:
            root_logger.critical("failed to create ds instance %s" % e)
        inf_fd.close()

    def load_pkcs12(self):
        dirname = dsinstance.config_dirname(self.serverid)
        dsdb = certs.CertDB(self.realm_name, nssdir=dirname)
        if self.pkcs12_info:
            dsdb.create_from_pkcs12(self.pkcs12_info[0], self.pkcs12_info[1])
            server_certs = dsdb.find_server_certs()
            if len(server_certs) == 0:
                raise RuntimeError("Could not find a suitable server cert in import in %s" % self.pkcs12_info[0])

            # We only handle one server cert
            self.nickname = server_certs[0][0]
            self.dercert = dsdb.get_cert_from_db(self.nickname, pem=False)
            dsdb.track_server_cert(self.nickname, self.principal, dsdb.passwd_fname)

    def create_certdb(self):
        """
        Create the dogtag 389-ds instance NSS certificate database. This needs
        to be done after dogtag is installed and configured.
        """
        dirname = dsinstance.config_dirname(self.serverid)
        dsdb = certs.CertDB(self.realm_name, nssdir=dirname, subject_base=self.subject_base)
        cadb = certs.CertDB(self.realm_name, host_name=self.fqdn, subject_base=self.subject_base)
        cadb.export_ca_cert('ipaCert', False)
        dsdb.create_from_cacert(cadb.cacert_fname, passwd=None)
        self.dercert = dsdb.create_server_cert("Server-Cert", self.fqdn, cadb)
        dsdb.track_server_cert("Server-Cert", self.principal, dsdb.passwd_fname)
        dsdb.create_pin_file()

    def enable_ssl(self):
        conn = ipaldap.IPAdmin("127.0.0.1", port=DEFAULT_DSPORT)
        conn.simple_bind_s("cn=directory manager", self.dm_password)

        mod = [(ldap.MOD_REPLACE, "nsSSLClientAuth", "allowed"),
               (ldap.MOD_REPLACE, "nsSSL3Ciphers",
                "-rsa_null_md5,+rsa_rc4_128_md5,+rsa_rc4_40_md5,+rsa_rc2_40_md5,\
+rsa_des_sha,+rsa_fips_des_sha,+rsa_3des_sha,+rsa_fips_3des_sha,+fortezza,\
+fortezza_rc4_128_sha,+fortezza_null,+tls_rsa_export1024_with_rc4_56_sha,\
+tls_rsa_export1024_with_des_cbc_sha")]
        conn.modify_s("cn=encryption,cn=config", mod)

        mod = [(ldap.MOD_ADD, "nsslapd-security", "on"),
               (ldap.MOD_ADD, "nsslapd-secureport", str(DEFAULT_DSPORT+1))]
        conn.modify_s("cn=config", mod)

        entry = ipaldap.Entry("cn=RSA,cn=encryption,cn=config")

        entry.setValues("objectclass", "top", "nsEncryptionModule")
        entry.setValues("cn", "RSA")
        entry.setValues("nsSSLPersonalitySSL", self.nickname)
        entry.setValues("nsSSLToken", "internal (software)")
        entry.setValues("nsSSLActivation", "on")

        conn.addEntry(entry)

        conn.unbind()

    def restart_instance(self):
        try:
            ipaservices.knownservices.dirsrv.restart(self.serverid)
            if not dsinstance.is_ds_running(self.serverid):
                root_logger.critical("Failed to restart the directory server. See the installation log for details.")
                sys.exit(1)
        except Exception:
            # TODO: roll back here?
            root_logger.critical("Failed to restart the directory server. See the installation log for details.")

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring CA directory server")

        enabled = self.restore_state("enabled")
        serverid = self.restore_state("serverid")

        # Just eat this state if it exists
        running = self.restore_state("running")

        if not enabled is None and not enabled:
            ipaservices.knownservices.dirsrv.disable()

        if not serverid is None:
            # drop the trailing / off the config_dirname so the directory
            # will match what is in certmonger
            dirname = dsinstance.config_dirname(serverid)[:-1]
            dsdb = certs.CertDB(self.realm_name, nssdir=dirname)
            dsdb.untrack_server_cert("Server-Cert")
            dsinstance.erase_ds_instance_data(serverid)

        user_exists = self.restore_state("user_exists")

        # At one time we removed this user on uninstall. That can potentially
        # orphan files, or worse, if another useradd runs in the intermim,
        # cause files to have a new owner.

class CAInstance(service.Service):
    """
    In the self-signed case the CA exists in the NSS_DB database.
    When using a dogtag CA the DS database contains just the
    server cert for DS. The mod_nss database will contain the RA agent
    cert that will be used to do authenticated requests against dogtag.

    This is done because we use python-nss and will inherit the opened
    NSS database in mod_python. In nsslib.py we do an nssinit but this will
    return success if the database is already initialized. It doesn't care
    if the database is different or not.

    external is a state machine:
       0 = not an externally signed CA
       1 = generating CSR to be signed
       2 = have signed cert, continue installation
    """

    def __init__(self, realm, ra_db):
        service.Service.__init__(self, "pki-cad")
        self.realm = realm
        self.dm_password = None
        self.admin_password = None
        self.fqdn = None
        self.pkcs12_info = None
        self.clone = False
        # for external CAs
        self.external = 0
        self.csr_file = None
        self.cert_file = None
        self.cert_chain_file = None
        self.create_ra_agent_db = True

        # The same database is used for mod_nss because the NSS context
        # will already have been initialized by Apache by the time
        # mod_python wants to do things.
        self.canickname = get_ca_nickname(realm)
        self.basedn = "o=ipaca"
        self.ca_agent_db = tempfile.mkdtemp(prefix = "tmp-")
        self.ra_agent_db = ra_db
        self.ra_agent_pwd = self.ra_agent_db + "/pwdfile.txt"
        self.ds_port = DEFAULT_DSPORT
        self.domain_name = "IPA"
        self.server_root = "/var/lib"
        self.ra_cert = None
        self.requestId = None

    def __del__(self):
        shutil.rmtree(self.ca_agent_db, ignore_errors=True)

    def is_installed(self):
        """
        Installing with an external CA is a two-step process. This
        is used to determine if the first step has been done.

        Returns True/False
        """
        return os.path.exists(self.server_root + '/' + PKI_INSTANCE_NAME)

    def configure_instance(self, host_name, dm_password,
                           admin_password, ds_port=DEFAULT_DSPORT,
                           pkcs12_info=None, master_host=None, csr_file=None,
                           cert_file=None, cert_chain_file=None,
                           subject_base=None):
        """Create a CA instance. This may involve creating the pki-ca instance
           dogtag instance.

           To create a clone, pass in pkcs12_info.

           Creating a CA with an external signer is a 2-step process. In
           step 1 we generate a CSR. In step 2 we are given the cert and
           chain and actually proceed to create the CA. For step 1 set
           csr_file. For step 2 set cert_file and cert_chain_file.
        """
        self.fqdn = host_name
        self.dm_password = dm_password
        self.admin_password = admin_password
        self.ds_port = ds_port
        self.pkcs12_info = pkcs12_info
        if self.pkcs12_info is not None:
            self.clone = True
        self.master_host = master_host
        if subject_base is None:
            self.subject_base = "O=%s" % self.realm
        else:
            self.subject_base = subject_base

        # Determine if we are installing as an externally-signed CA and
        # what stage we're in.
        if csr_file is not None:
            self.csr_file=csr_file
            self.external=1
        elif cert_file is not None:
            self.cert_file=cert_file
            self.cert_chain_file=cert_chain_file
            self.external=2

        self.step("creating certificate server user", self.__create_ca_user)
        if not ipautil.dir_exists("/var/lib/pki-ca"):
            self.step("creating pki-ca instance", self.create_instance)
        self.step("configuring certificate server instance", self.__configure_instance)
        self.step("disabling nonces", self.__disable_nonce)
        # Step 1 of external is getting a CSR so we don't need to do these
        # steps until we get a cert back from the external CA.
        if self.external != 1:
            if not self.clone:
                self.step("creating CA agent PKCS#12 file in /root", self.__create_ca_agent_pkcs12)
            if self.create_ra_agent_db:
                self.step("creating RA agent certificate database", self.__create_ra_agent_db)
            self.step("importing CA chain to RA certificate database", self.__import_ca_chain)
            self.step("fixing RA database permissions", self.fix_ra_perms)
            self.step("setting up signing cert profile", self.__setup_sign_profile)
            self.step("set up CRL publishing", self.__enable_crl_publish)
            self.step("set certificate subject base", self.__set_subject_in_config)
            self.step("enabling Subject Key Identifier", self.enable_subject_key_identifier)
            self.step("configuring certificate server to start on boot", self.__enable)
            if not self.clone:
                self.step("restarting certificate server", self.__restart_instance)
                self.step("requesting RA certificate from CA", self.__request_ra_certificate)
                self.step("issuing RA agent certificate", self.__issue_ra_cert)
                self.step("adding RA agent as a trusted user", self.__configure_ra)
            self.step("Configure HTTP to proxy connections", self.__http_proxy)

        self.start_creation("Configuring certificate server", 210)

    def create_instance(self):
        """
        If for some reason the instance doesn't exist, create a new one."
        """

        args = ['/usr/bin/pkicreate',
                '-pki_instance_root', '/var/lib',
                '-pki_instance_name', PKI_INSTANCE_NAME,
                '-subsystem_type', 'ca',
                '-agent_secure_port', str(AGENT_SECURE_PORT),
                '-ee_secure_port', str(EE_SECURE_PORT),
                '-admin_secure_port', str(ADMIN_SECURE_PORT),
                '-ee_secure_client_auth_port', str(EE_CLIENT_AUTH_PORT),
                '-unsecure_port', str(UNSECURE_PORT),
                '-tomcat_server_port', str(TOMCAT_SERVER_PORT),
                '-redirect', 'conf=/etc/pki-ca',
                '-redirect', 'logs=/var/log/pki-ca',
                '-enable_proxy'
        ]
        ipautil.run(args, env={'PKI_HOSTNAME':self.fqdn})

    def __enable(self):
        self.backup_state("enabled", self.is_enabled())
        # We do not let the system start IPA components on its own,
        # Instead we reply on the IPA init script to start only enabled
        # components as found in our LDAP configuration tree
        # We need to install DS before we can actually ldap_enable a service.
        # so actual enablement is delayed.

    def __create_ca_user(self):
        try:
            pwd.getpwnam(PKI_USER)
            root_logger.debug("ca user %s exists" % PKI_USER)
        except KeyError:
            root_logger.debug("adding ca user %s" % PKI_USER)
            args = ["/usr/sbin/useradd", "-c", "CA System User",
                                         "-d", "/var/lib",
                                         "-s", "/sbin/nologin",
                                         "-M", "-r", PKI_USER]
            try:
                ipautil.run(args)
                root_logger.debug("done adding user")
            except ipautil.CalledProcessError, e:
                root_logger.critical("failed to add user %s" % e)

    def __configure_instance(self):
        preop_pin = get_preop_pin(self.server_root, PKI_INSTANCE_NAME)

        try:
            args = ["/usr/bin/perl", "/usr/bin/pkisilent",  "ConfigureCA",
                    "-cs_hostname", self.fqdn,
                    "-cs_port", str(ADMIN_SECURE_PORT),
                    "-client_certdb_dir", self.ca_agent_db,
                    "-client_certdb_pwd", self.admin_password,
                    "-preop_pin" , preop_pin,
                    "-domain_name", self.domain_name,
                    "-admin_user", "admin",
                    "-admin_email",  "root@localhost",
                    "-admin_password", self.admin_password,
                    "-agent_name", "ipa-ca-agent",
                    "-agent_key_size", "2048",
                    "-agent_key_type", "rsa",
                    "-agent_cert_subject", "CN=ipa-ca-agent,%s" % self.subject_base,
                    "-ldap_host", self.fqdn,
                    "-ldap_port", str(self.ds_port),
                    "-bind_dn", "cn=Directory Manager",
                    "-bind_password", self.dm_password,
                    "-base_dn", self.basedn,
                    "-db_name", "ipaca",
                    "-key_size", "2048",
                    "-key_type", "rsa",
                    "-key_algorithm", "SHA256withRSA",
                    "-save_p12", "true",
                    "-backup_pwd", self.admin_password,
                    "-subsystem_name", self.service_name,
                    "-token_name", "internal",
                    "-ca_subsystem_cert_subject_name", "CN=CA Subsystem,%s" % self.subject_base,
                    "-ca_ocsp_cert_subject_name", "CN=OCSP Subsystem,%s" % self.subject_base,
                    "-ca_server_cert_subject_name", "CN=%s,%s" % (self.fqdn, self.subject_base),
                    "-ca_audit_signing_cert_subject_name", "CN=CA Audit,%s" % self.subject_base,
                    "-ca_sign_cert_subject_name", "CN=Certificate Authority,%s" % self.subject_base ]
            if self.external == 1:
                args.append("-external")
                args.append("true")
                args.append("-ext_csr_file")
                args.append(self.csr_file)
            elif self.external == 2:
                args.append("-external")
                args.append("true")
                args.append("-ext_ca_cert_file")
                args.append(self.cert_file)
                args.append("-ext_ca_cert_chain_file")
                args.append(self.cert_chain_file)
            else:
                args.append("-external")
                args.append("false")
            if (self.clone):
                """sd = security domain -->  all CS systems get registered to
                   a security domain. This is set to the hostname and port of
                   the master CA.
                """
                # The install wizard expects the file to be here.
                cafile = self.pkcs12_info[0]
                shutil.copy(cafile, "/var/lib/pki-ca/alias/ca.p12")
                pent = pwd.getpwnam(PKI_USER)
                os.chown("/var/lib/pki-ca/alias/ca.p12", pent.pw_uid, pent.pw_gid )
                args.append("-clone")
                args.append("true")
                args.append("-clone_p12_file")
                args.append("ca.p12")
                args.append("-clone_p12_password")
                args.append(self.dm_password)
                args.append("-sd_hostname")
                args.append(self.master_host)
                args.append("-sd_admin_port")
                args.append("443")
                args.append("-sd_admin_name")
                args.append("admin")
                args.append("-sd_admin_password")
                args.append(self.admin_password)
                args.append("-clone_start_tls")
                args.append("true")
                args.append("-clone_uri")
                args.append("https://%s" % ipautil.format_netloc(self.master_host, 443))
            else:
                args.append("-clone")
                args.append("false")

            # Define the things we don't want logged
            nolog = (self.admin_password, self.dm_password,)

            ipautil.run(args, env={'PKI_HOSTNAME':self.fqdn}, nolog=nolog)
        except ipautil.CalledProcessError, e:
            root_logger.critical("failed to configure ca instance %s" % e)
            raise RuntimeError('Configuration of CA failed')

        if self.external == 1:
            print "The next step is to get %s signed by your CA and re-run ipa-server-install as:" % self.csr_file
            print "ipa-server-install --external_cert_file=/path/to/signed_certificate --external_ca_file=/path/to/external_ca_certificate"
            sys.exit(0)

        # pkisilent makes a copy of the CA PKCS#12 file for us but gives
        # it a lousy name.
        if ipautil.file_exists("/root/tmp-ca.p12"):
            shutil.move("/root/tmp-ca.p12", "/root/cacert.p12")

        root_logger.debug("completed creating ca instance")

    def __restart_instance(self):
        try:
            self.restart(PKI_INSTANCE_NAME)
            installutils.wait_for_open_ports('localhost', 9180, 300)
        except Exception:
            # TODO: roll back here?
            root_logger.critical("Failed to restart the certificate server. See the installation log for details.")

    def __disable_nonce(self):
        # Turn off Nonces
        if installutils.update_file('/var/lib/pki-ca/conf/CS.cfg', 'ca.enableNonces=true', 'ca.enableNonces=false') != 0:
            raise RuntimeError("Disabling nonces failed")
        pent = pwd.getpwnam(PKI_USER)
        os.chown('/var/lib/pki-ca/conf/CS.cfg', pent.pw_uid, pent.pw_gid )

    def __issue_ra_cert(self):
        # The CA certificate is in the agent DB but isn't trusted
        (admin_fd, admin_name) = tempfile.mkstemp()
        os.write(admin_fd, self.admin_password)
        os.close(admin_fd)

        # Look thru the cert chain to get all the certs we need to add
        # trust for
        p = subprocess.Popen(["/usr/bin/certutil", "-d", self.ca_agent_db,
                              "-O", "-n", "ipa-ca-agent"], stdout=subprocess.PIPE)

        chain = p.stdout.read()
        chain = chain.split("\n")

        root_nickname=[]
        for i in xrange(len(chain)):
            m = re.match('\ *"(.*)" \[.*', chain[i])
            if m:
                nick = m.groups(0)[0]
                if nick != "ipa-ca-agent" and nick[:7] != "Builtin":
                    root_nickname.append(m.groups()[0])

        try:
            for nick in root_nickname:
                self.__run_certutil(
                    ['-M', '-t', 'CT,C,C', '-n',
                     nick],
                     database=self.ca_agent_db, pwd_file=self.admin_password)
        finally:
            os.remove(admin_name)

        # Retrieve the certificate request so we can get the values needed
        # to issue a certificate. Use sslget here because this is a
        # temporary database and nsslib doesn't currently support gracefully
        # opening and closing an NSS database. This would leave the installer
        # process stuck using this database during the entire cycle. We need
        # to use the final RA agent database when issuing certs for DS and
        # mod_nss.
        args = [
            '/usr/bin/sslget',
            '-v',
            '-n', 'ipa-ca-agent',
            '-p', self.admin_password,
            '-d', self.ca_agent_db,
            '-r', '/ca/agent/ca/profileReview?requestId=%s' % self.requestId,
            '%s' % ipautil.format_netloc(self.fqdn, AGENT_SECURE_PORT),
        ]
        (stdout, stderr, returncode) = ipautil.run(args, nolog=(self.admin_password,))

        data = stdout.split('\r\n')
        params = get_defList(data)
        params['requestId'] = find_substring(data, "requestId")
        params['op'] = 'approve'
        params['submit'] = 'submit'
        params['requestNotes'] = ''
        params = urllib.urlencode(params)

        # Now issue the RA certificate.
        args = [
            '/usr/bin/sslget',
            '-v',
            '-n', 'ipa-ca-agent',
            '-p', self.admin_password,
            '-d', self.ca_agent_db,
            '-e', params,
            '-r', '/ca/agent/ca/profileProcess',
            '%s' % ipautil.format_netloc(self.fqdn, AGENT_SECURE_PORT),
        ]
        (stdout, stderr, returncode) = ipautil.run(args, nolog=(self.admin_password,))

        data = stdout.split('\r\n')
        outputList = get_outputList(data)

        self.ra_cert = outputList['b64_cert']
        self.ra_cert = self.ra_cert.replace('\\n','')
        self.ra_cert = x509.strip_header(self.ra_cert)

        # Add the new RA cert to the database in /etc/httpd/alias
        (agent_fd, agent_name) = tempfile.mkstemp()
        os.write(agent_fd, self.ra_cert)
        os.close(agent_fd)
        try:
            self.__run_certutil(
                ['-A', '-t', 'u,u,u', '-n', 'ipaCert', '-a',
                 '-i', agent_name]
            )
        finally:
            os.remove(agent_name)

    def import_ra_cert(self, rafile):
        """
        Cloned RAs will use the same RA agent cert as the master so we
        need to import from a PKCS#12 file.

        Used when setting up replication
        """
        # Add the new RA cert to the database in /etc/httpd/alias
        (agent_fd, agent_name) = tempfile.mkstemp()
        os.write(agent_fd, self.dm_password)
        os.close(agent_fd)
        try:
            import_pkcs12(rafile, agent_name, self.ra_agent_db, self.ra_agent_pwd)
        finally:
            os.remove(agent_name)

    def __configure_ra(self):
        # Create an RA user in the CA LDAP server and add that user to
        # the appropriate groups so it can issue certificates without
        # manual intervention.
        ld = ldap.initialize("ldap://%s" % ipautil.format_netloc(self.fqdn, self.ds_port))
        ld.protocol_version=ldap.VERSION3
        ld.simple_bind_s("cn=Directory Manager", self.dm_password)

        decoded = base64.b64decode(self.ra_cert)

        entry_dn = "uid=%s,ou=People,%s" % ("ipara", self.basedn)
        entry = [
        ('objectClass', ['top', 'person', 'organizationalPerson', 'inetOrgPerson', 'cmsuser']),
        ('uid', "ipara"),
        ('sn', "ipara"),
        ('cn', "ipara"),
        ('usertype', "agentType"),
        ('userstate', "1"),
        ('userCertificate', decoded),
        ('description', '2;%s;CN=Certificate Authority,%s;CN=IPA RA,%s' % (str(self.requestId), self.subject_base, self.subject_base)),]

        ld.add_s(entry_dn, entry)

        dn = "cn=Certificate Manager Agents,ou=groups,%s" % self.basedn
        modlist = [(0, 'uniqueMember', '%s' % entry_dn)]
        ld.modify_s(dn, modlist)

        dn = "cn=Registration Manager Agents,ou=groups,%s" % self.basedn
        modlist = [(0, 'uniqueMember', '%s' % entry_dn)]
        ld.modify_s(dn, modlist)

        ld.unbind_s()

    def __run_certutil(self, args, database=None, pwd_file=None,stdin=None):
        if not database:
            database = self.ra_agent_db
        if not pwd_file:
            pwd_file = self.ra_agent_pwd
        new_args = ["/usr/bin/certutil", "-d", database, "-f", pwd_file]
        new_args = new_args + args
        return ipautil.run(new_args, stdin, nolog=(pwd_file,))

    def __create_ra_agent_db(self):
        if ipautil.file_exists(self.ra_agent_db + "/cert8.db"):
            ipautil.backup_file(self.ra_agent_db + "/cert8.db")
            ipautil.backup_file(self.ra_agent_db + "/key3.db")
            ipautil.backup_file(self.ra_agent_db + "/secmod.db")
            ipautil.backup_file(self.ra_agent_db + "/pwdfile.txt")

        if not ipautil.dir_exists(self.ra_agent_db):
            os.mkdir(self.ra_agent_db)

        # Create the password file for this db
        hex_str = binascii.hexlify(os.urandom(10))
        f = os.open(self.ra_agent_pwd, os.O_CREAT | os.O_RDWR)
        os.write(f, hex_str)
        os.close(f)
        os.chmod(self.ra_agent_pwd, stat.S_IRUSR)

        (stdout, stderr, returncode)  = self.__run_certutil(["-N"])

    def __get_ca_chain(self):
        try:
            return dogtag.get_ca_certchain(ca_host=self.fqdn)
        except Exception, e:
            raise RuntimeError("Unable to retrieve CA chain: %s" % str(e))

    def __create_ca_agent_pkcs12(self):
        (pwd_fd, pwd_name) = tempfile.mkstemp()
        os.write(pwd_fd, self.admin_password)
        os.close(pwd_fd)
        try:
            ipautil.run(["/usr/bin/pk12util",
                         "-n", "ipa-ca-agent",
                         "-o", "/root/ca-agent.p12",
                         "-d", self.ca_agent_db,
                         "-k", pwd_name,
                         "-w", pwd_name])
        finally:
            os.remove(pwd_name)

    def __import_ca_chain(self):
        chain = self.__get_ca_chain()

        # If this chain contains multiple certs then certutil will only import
        # the first one. So we have to pull them all out and import them
        # separately. Unfortunately no NSS tool can do this so we have to
        # use openssl.

        # Convert to DER because the chain comes back as one long string which
        # makes openssl throw up.
        data = base64.b64decode(chain)

        (certlist, stderr, returncode) = ipautil.run(["/usr/bin/openssl",
             "pkcs7",
             "-inform",
             "DER",
             "-print_certs",
             ], stdin=data)

        # Ok, now we have all the certificates in certs, walk thru it
        # and pull out each certificate and add it to our database

        st = 1
        en = 0
        subid = 0
        ca_dn = DN(('CN','Certificate Authority'), self.subject_base)
        while st > 0:
            st = certlist.find('-----BEGIN', en)
            en = certlist.find('-----END', en+1)
            if st > 0:
                try:
                    (chain_fd, chain_name) = tempfile.mkstemp()
                    os.write(chain_fd, certlist[st:en+25])
                    os.close(chain_fd)
                    (rdn, subject_dn) = certs.get_cert_nickname(certlist[st:en+25])
                    if subject_dn == ca_dn:
                        nick = get_ca_nickname(self.realm)
                    else:
                        nick = str(subject_dn)
                    self.__run_certutil(
                        ['-A', '-t', 'CT,C,C', '-n', nick, '-a',
                         '-i', chain_name]
                    )
                finally:
                    os.remove(chain_name)
                    subid = subid + 1

    def __request_ra_certificate(self):
        # Create a noise file for generating our private key
        noise = array.array('B', os.urandom(128))
        (noise_fd, noise_name) = tempfile.mkstemp()
        os.write(noise_fd, noise)
        os.close(noise_fd)

        # Generate our CSR. The result gets put into stdout
        try:
            (stdout, stderr, returncode) = self.__run_certutil(["-R", "-k", "rsa", "-g", "2048", "-s", "CN=IPA RA,%s" % self.subject_base, "-z", noise_name, "-a"])
        finally:
            os.remove(noise_name)

        csr = pkcs10.strip_header(stdout)

        # Send the request to the CA
        conn = httplib.HTTPConnection(self.fqdn, 9180)
        params = urllib.urlencode({'profileId': 'caServerCert',
                'cert_request_type': 'pkcs10',
                'requestor_name': 'IPA Installer',
                'cert_request': csr,
                'xmlOutput': 'true'})
        headers = {"Content-type": "application/x-www-form-urlencoded",
                   "Accept": "text/plain"}

        conn.request("POST", "/ca/ee/ca/profileSubmit", params, headers)
        res = conn.getresponse()
        if res.status == 200:
            data = res.read()
            conn.close()
            doc = xml.dom.minidom.parseString(data)
            item_node = doc.getElementsByTagName("RequestId")
            self.requestId = item_node[0].childNodes[0].data
            doc.unlink()
            self.requestId = self.requestId.strip()
            if self.requestId is None:
                raise RuntimeError("Unable to determine RA certificate requestId")
        else:
            conn.close()
            raise RuntimeError("Unable to submit RA cert request")

    def fix_ra_perms(self):
        os.chmod(self.ra_agent_db + "/cert8.db", 0640)
        os.chmod(self.ra_agent_db + "/key3.db", 0640)
        os.chmod(self.ra_agent_db + "/secmod.db", 0640)

        pent = pwd.getpwnam("apache")
        os.chown(self.ra_agent_db + "/cert8.db", 0, pent.pw_gid )
        os.chown(self.ra_agent_db + "/key3.db", 0, pent.pw_gid )
        os.chown(self.ra_agent_db + "/secmod.db", 0, pent.pw_gid )
        os.chown(self.ra_agent_pwd, pent.pw_uid, pent.pw_gid)

    def __setup_sign_profile(self):
        # Tell the profile to automatically issue certs for RAs
        installutils.set_directive('/var/lib/pki-ca/profiles/ca/caJarSigningCert.cfg', 'auth.instance_id', 'raCertAuth', quotes=False, separator='=')

    def __enable_crl_publish(self):
        """
        Enable file-based CRL publishing and disable LDAP publishing.

        http://www.redhat.com/docs/manuals/cert-system/8.0/admin/html/Setting_up_Publishing.html
        """
        caconfig = "/var/lib/pki-ca/conf/CS.cfg"

        publishdir='/var/lib/pki-ca/publish'
        os.mkdir(publishdir)
        os.chmod(publishdir, 0755)
        pent = pwd.getpwnam(PKI_USER)
        os.chown(publishdir, pent.pw_uid, pent.pw_gid)

        # Enable file publishing, disable LDAP
        installutils.set_directive(caconfig, 'ca.publish.enable', 'true', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.ldappublish.enable', 'false', quotes=False, separator='=')

        # Create the file publisher, der only, not b64
        installutils.set_directive(caconfig, 'ca.publish.publisher.impl.FileBasedPublisher.class','com.netscape.cms.publish.publishers.FileBasedPublisher', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.crlLinkExt', 'bin', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.directory', publishdir, quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.latestCrlLink', 'true', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.pluginName', 'FileBasedPublisher', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.timeStamp', 'LocalTime', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.zipCRLs', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.zipLevel', '9', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.Filename.b64', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.publisher.instance.FileBaseCRLPublisher.Filename.der', 'true', quotes=False, separator='=')

        # The publishing rule
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.enable', 'true', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.mapper', 'NoMap', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.pluginName', 'Rule', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.predicate=', '', quotes=False, separator='')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.publisher', 'FileBaseCRLPublisher', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.FileCrlRule.type', 'crl', quotes=False, separator='=')

        # Now disable LDAP publishing
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapCaCertRule.enable', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapCrlRule.enable', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapUserCertRule.enable', 'false', quotes=False, separator='=')
        installutils.set_directive(caconfig, 'ca.publish.rule.instance.LdapXCertRule.enable', 'false', quotes=False, separator='=')

        # Fix the CRL URI in the profile
        installutils.set_directive(IPA_SERVICE_PROFILE,
            'policyset.serverCertSet.9.default.params.crlDistPointsPointName_0',
            'https://%s/ipa/crl/MasterCRL.bin' % ipautil.format_netloc(self.fqdn),
            quotes=False, separator='=')

        ipaservices.restore_context(publishdir)

    def __set_subject_in_config(self):
        # dogtag ships with an IPA-specific profile that forces a subject
        # format. We need to update that template with our base subject
        if installutils.update_file(IPA_SERVICE_PROFILE, 'OU=pki-ipa, O=IPA', self.subject_base):
            print "Updating subject_base in CA template failed"

    def uninstall(self):
        if self.is_configured():
            self.print_msg("Unconfiguring CA")

        enabled = self.restore_state("enabled")
        if not enabled is None and not enabled:
            self.disable()

        try:
            ipautil.run(["/usr/bin/pkiremove", "-pki_instance_root=/var/lib",
                         "-pki_instance_name=%s" % PKI_INSTANCE_NAME, "--force"])
        except ipautil.CalledProcessError, e:
            root_logger.critical("failed to uninstall CA instance %s" % e)

        # At one time we removed this user on uninstall. That can potentially
        # orphan files, or worse, if another useradd runs in the intermim,
        # cause files to have a new owner.
        user_exists = self.restore_state("user_exists")

    def publish_ca_cert(self, location):
        args = ["-L", "-n", self.canickname, "-a"]
        (cert, err, returncode) = self.__run_certutil(args)
        fd = open(location, "w+")
        fd.write(cert)
        fd.close()
        os.chmod(location, 0444)

    def __http_proxy(self):
        shutil.copy(ipautil.SHARE_DIR + "ipa-pki-proxy.conf",
                    HTTPD_CONFD + "ipa-pki-proxy.conf")

    def enable_subject_key_identifier(self):
        """
        See if Subject Key Identifier is set in the profile and if not, add it.
        """
        setlist = installutils.get_directive(IPA_SERVICE_PROFILE,
            'policyset.serverCertSet.list', separator='=')

        # this is the default setting from pki-ca. Don't touch it if a user
        # has manually modified it.
        if setlist == '1,2,3,4,5,6,7,8':
            installutils.set_directive(IPA_SERVICE_PROFILE,
                'policyset.serverCertSet.list',
                '1,2,3,4,5,6,7,8,10',
                quotes=False, separator='=')
            installutils.set_directive(IPA_SERVICE_PROFILE,
                'policyset.serverCertSet.10.constraint.class_id',
                'noConstraintImpl',
                quotes=False, separator='=')
            installutils.set_directive(IPA_SERVICE_PROFILE,
                'policyset.serverCertSet.10.constraint.name',
                'No Constraint',
                quotes=False, separator='=')
            installutils.set_directive(IPA_SERVICE_PROFILE,
                'policyset.serverCertSet.10.default.class_id',
                'subjectKeyIdentifierExtDefaultImpl',
                quotes=False, separator='=')
            installutils.set_directive(IPA_SERVICE_PROFILE,
                'policyset.serverCertSet.10.default.name',
                'Subject Key Identifier Extension Default',
                quotes=False, separator='=')
            installutils.set_directive(IPA_SERVICE_PROFILE,
                'policyset.serverCertSet.10.default.params.critical',
                'false',
                quotes=False, separator='=')
            return True

        # No update was done
        return False

def install_replica_ca(config, postinstall=False):
    """
    Install a CA on a replica.

    There are two modes of doing this controlled:
      - While the replica is being installed
      - Post-replica installation

    config is a ReplicaConfig object

    Returns a tuple of the CA and CADS instances
    """
    cafile = config.dir + "/cacert.p12"

    if not ipautil.file_exists(cafile):
        # self-signed replica
        return (None, None)

    if not config.setup_ca:
        # We aren't configuring the CA in this step but we still need
        # a minimum amount of information on the CA for this IPA install.
        ca = CAInstance(config.realm_name, certs.NSS_DIR)
        ca.dm_password = config.dirman_password
        ca.subject_base = config.subject_base
        return (ca, None)

    ca = CAInstance(config.realm_name, certs.NSS_DIR)
    ca.dm_password = config.dirman_password
    ca.subject_base = config.subject_base
    if ca.is_installed():
        sys.exit("A CA is already configured on this system.")

    pkcs12_info = None
    if ipautil.file_exists(config.dir + "/dogtagcert.p12"):
        pkcs12_info = (config.dir + "/dogtagcert.p12",
                       config.dir + "/dirsrv_pin.txt")
    cs = CADSInstance()
    cs.create_instance(config.realm_name, config.host_name,
                       config.domain_name, config.dirman_password,
                       pkcs12_info)
    cs.load_pkcs12()
    cs.enable_ssl()
    cs.restart_instance()
    ca = CAInstance(config.realm_name, certs.NSS_DIR)
    if postinstall:
        # If installing this afterward the Apache NSS database already
        # exists, don't remove it.
        ca.create_ra_agent_db = False
    ca.configure_instance(config.host_name, config.dirman_password,
                          config.dirman_password, pkcs12_info=(cafile,),
                          master_host=config.master_host_name,
                          subject_base=config.subject_base)

    # The dogtag DS instance needs to be restarted after installation.
    # The procedure for this is: stop dogtag, stop DS, start DS, start
    # dogtag
    #
    #
    # The service_name trickery is due to the service naming we do
    # internally. In the case of the dogtag DS the name doesn't match the
    # unix service.

    service.print_msg("Restarting the directory and certificate servers")
    ca.stop()
    ipaservices.knownservices.dirsrv.stop("PKI-IPA")
    ipaservices.knownservices.dirsrv.start("PKI-IPA")
    ca.start()

    return (ca, cs)

if __name__ == "__main__":
    standard_logging_setup("install.log")
    cs = CADSInstance()
    cs.create_instance("EXAMPLE.COM", "catest.example.com", "example.com", "password")
    ca = CAInstance("EXAMPLE.COM", "/etc/httpd/alias")
    ca.configure_instance("catest.example.com", "password", "password")
