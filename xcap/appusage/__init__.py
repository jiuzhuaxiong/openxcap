"""XCAP application usage module"""

import os

from cStringIO import StringIO
from lxml import etree

from application.configuration import *
from application.configuration.datatypes import StringList
from application.process import process
from application import log

from twisted.internet import defer
from twisted.python import failure

from xcap.errors import *
from xcap.interfaces.backend import StatusResponse
from xcap.element import XCAPElement

supported_applications = ('xcap-caps', 'pres-rules', 'org.openmobilealliance.pres-rules',
                          'resource-lists', 'rls-services', 'pidf-manipulation', 'watchers')

class EnabledApplications(StringList):
    def __new__(typ, value):
        apps = StringList.__new__(typ, value)
        if len(apps) == 1 and apps[0] == "all":
            return supported_applications
        for app in apps:
            if app not in supported_applications:
                log.warn("ignoring unknown application : %s" % app)
                apps.remove(app)
        return apps

class Backend(object):
    """Configuration datatype, used to select a backend module from the configuration file."""
    def __new__(typ, value):
        try:
            return __import__('xcap.interfaces.backend.%s' % value.lower(), globals(), locals(), [''])
        except ImportError, e:
            raise ValueError("Couldn't find the '%s' backend module: %s" % (value.lower(), str(e)))

class ServerConfig(ConfigSection):
    _datatypes = {'applications': EnabledApplications, 'backend': Backend}
    applications = EnabledApplications("all")
    backend = Backend('Database')
    document_validation = True

## We use this to overwrite some of the settings above on a local basis if needed
configuration = ConfigFile('config.ini')
configuration.read_settings('Server', ServerConfig)

schemas_directory = os.path.join(os.path.dirname(globals()["__file__"]), "../", "xml-schemas")

class ApplicationUsage(object):
    """Base class defining an XCAP application"""
    id = None                ## the Application Unique ID (AUID)
    default_ns = None        ## the default XML namespace
    mime_type = None         ## the MIME type
    schema_file = None       ## filename of the schema for the application
    
    def __init__(self, storage):
        ## the XML schema that defines valid documents for this application
        if self.schema_file:
            xml_schema_doc = etree.parse(open(os.path.join(schemas_directory, self.schema_file), 'r'))
            self.xml_schema = etree.XMLSchema(xml_schema_doc)
        else:
            class EverythingIsValid:
                def validate(self, *args, **kw):
                    return True
            self.xml_schema = EverythingIsValid()
        if storage is not None:
            self.storage = storage

    ## Validation

    def _check_UTF8_encoding(self, xml_doc):
        """Check if the document is UTF8 encoded. Raise an NotUTF8Error if it's not."""
        if xml_doc.docinfo.encoding != 'UTF-8':
            log.error("The document is not UTF-8 encoded. Encoding is : %s" % xml_doc.docinfo.encoding)
            raise NotUTF8Error()

    def _check_schema_validation(self, xml_doc):
        """Check if the given XCAP document validates against the application's schema"""
        if not self.xml_schema.validate(xml_doc):
            log.error("Failed to validate document against XML schema: %s" % self.xml_schema.error_log)
            raise SchemaValidationError("The document doesn't comply to the XML schema")

    def _check_additional_constraints(self, xml_doc):
        """Check additional validations constraints for this XCAP document. Should be 
           overriden in subclasses if specified by the application usage, and raise
           a ConstraintFailureError if needed."""
        pass

    def validate_document(self, xcap_doc):
        """Check if a document is valid for this application."""
        try:
            xml_doc = etree.parse(StringIO(xcap_doc))
        except: ## not a well formed XML document
            log.error("XML document is not well formed.")
            raise NotWellFormedError
        self._check_UTF8_encoding(xml_doc)
        if ServerConfig.document_validation:
            self._check_schema_validation(xml_doc)
        self._check_additional_constraints(xml_doc)

    ## Authorization policy

    def is_authorized(self, xcap_user, xcap_uri):
        """Default authorization policy. Authorizes an XCAPUser for an XCAPUri.
           Return True if the user is authorized, False otherwise."""
        if xcap_user and xcap_user == xcap_uri.user:
            return True
        return False

    ## Document management

    def get_document(self, uri, check_etag):
        return self.storage.get_document(uri, check_etag)

    def put_document(self, uri, document, check_etag):
        try:
            self.validate_document(document)
        except Exception:
            return defer.fail(failure.Failure())
        return self.storage.put_document(uri, document, check_etag)

    def delete_document(self, uri, check_etag):
        return self.storage.delete_document(uri, check_etag)

    ## Element management

    def _cb_put_element(self, response, uri, element, check_etag):
        """This is called when the document that relates to the element is retreived."""
        if response.code == 404:
            raise NoParentError

        fixed_element_selector = uri.node_selector.element_selector.fix_star(element)

        result = XCAPElement.put(response.data, fixed_element_selector, element)
        if result is None:
            raise NoParentError # vs. ResourceNotFound?

        new_document, created = result
        get_result = XCAPElement.get(new_document, uri.node_selector.element_selector)

        if get_result != element.strip():
            # GET request on the same URI must return just put document. This PUT doesn't comply.
            raise CannotInsertError

        d = self.put_document(uri, new_document, check_etag)

        def set_201_code(response):
            try:
                if response.code==200:
                    response.code = 201
            except AttributeError:
                pass
            return response

        if created:
            d.addCallback(set_201_code)
        
        return d

    def put_element(self, uri, element, check_etag):
        try:
            etree.parse(StringIO(element)).getroot()
            # verify if it has one element, if not should we throw the same exception?
        except:
            raise NotXMLFragmentError
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_put_element, callbackArgs=(uri, element, check_etag))

    def _cb_get_element(self, response, uri):
        """This is called when the document that relates to the element is retreived."""
        if response.code == 404:
            raise ResourceNotFound
        result = XCAPElement.get(response.data, uri.node_selector.element_selector)
        if not result:
            raise ResourceNotFound
        return StatusResponse(200, response.etag, result)

    def get_element(self, uri, check_etag):
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_get_element, callbackArgs=(uri, ))

    def _cb_delete_element(self, response, uri, check_etag):
        if response.code == 404:
            raise ResourceNotFound
        new_document = XCAPElement.delete(response.data, uri.node_selector.element_selector)
        if not new_document:
            raise ResourceNotFound
        get_result = XCAPElement.find(new_document, uri.node_selector.element_selector)
        if get_result:
            # GET request on the same URI must return 404. This DELETE doesn't comply.
            raise CannotDeleteError
        return self.put_document(uri, new_document, check_etag)

    def delete_element(self, uri, check_etag):
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_delete_element, callbackArgs=(uri, check_etag))

    ## Attribute management
    
    def _cb_get_attribute(self, response, uri):
        """This is called when the document that relates to the attribute is retreived."""
        if response.code == 404:
            raise ResourceNotFound
        document = response.data
        xml_doc = etree.parse(StringIO(document))
        application = getApplicationForURI(uri)
        ns_dict = uri.node_selector.get_ns_bindings(application.default_ns)
        try:
            xpath = uri.node_selector.replace_default_prefix()
            attribute = xml_doc.xpath(xpath, namespaces = ns_dict)
        except:
            raise ResourceNotFound
        if len(attribute) != 1:
            raise ResourceNotFound
        # TODO
        # The server MUST NOT add namespace bindings representing namespaces 
        # used by the element or its children, but declared in ancestor elements
        return StatusResponse(200, response.etag, attribute[0])

    def get_attribute(self, uri, check_etag):
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_get_attribute, callbackArgs=(uri, ))

    def _cb_delete_attribute(self, response, uri, check_etag):
        if response.code == 404:
            raise ResourceNotFound
        document = response.data
        xml_doc = etree.parse(StringIO(document))        
        application = getApplicationForURI(uri)
        ns_dict = uri.node_selector.get_ns_bindings(application.default_ns)
        try:
            elem = xml_doc.xpath(uri.node_selector.replace_default_prefix(append_terminal=False),namespaces=ns_dict)
        except:
            raise ResourceNotFound
        if len(elem) != 1:
            raise ResourceNotFound
        elem = elem[0]
        attribute = uri.node_selector.terminal_selector.attribute
        if elem.get(attribute):  ## check if the attribute exists XXX use KeyError instead
            del elem.attrib[attribute]
        else:
            raise ResourceNotFound
        new_document = etree.tostring(xml_doc, encoding='UTF-8', xml_declaration=True)
        return self.put_document(uri, new_document, check_etag)

    def delete_attribute(self, uri, check_etag):
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_delete_attribute, callbackArgs=(uri, check_etag))

    def _cb_put_attribute(self, response, uri, attribute, check_etag):
        """This is called when the document that relates to the element is retreived."""
        if response.code == 404:
            raise NoParentError
        document = response.data
        xml_doc = etree.parse(StringIO(document))
        application = getApplicationForURI(uri)
        ns_dict = uri.node_selector.get_ns_bindings(application.default_ns)
        try:
            elem = xml_doc.xpath(uri.node_selector.replace_default_prefix(append_terminal=False),namespaces=ns_dict)
        except:
            raise NoParentError
        if len(elem) != 1:
            raise NoParentError
        elem = elem[0]
        attr_name = uri.node_selector.terminal_selector.attribute
        elem.set(attr_name, attribute)
        new_document = etree.tostring(xml_doc, encoding='UTF-8', xml_declaration=True)
        return self.put_document(uri, new_document, check_etag)

    def put_attribute(self, uri, attribute, check_etag):
        ## TODO verifica daca atributul e valid
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_put_attribute, callbackArgs=(uri, attribute, check_etag))

    ## Namespace Bindings
    
    def _cb_get_ns_bindings(self, response, uri):
        """This is called when the document that relates to the element is retreived."""
        if response.code == 404:
            raise ResourceNotFound
        document = response.data
        xml_doc = etree.parse(StringIO(document))
        application = getApplicationForURI(uri)
        ns_dict = uri.node_selector.get_ns_bindings(application.default_ns)
        try:
            elem = xml_doc.xpath(uri.node_selector.replace_default_prefix(append_terminal=False),namespaces=ns_dict)
        except:
            raise ResourceNotFound
        if not elem:
            raise ResourceNotFound
        elem = elem[0]
        namespaces = ''
        for prefix, ns in elem.nsmap.items():
            namespaces += ' xmlns%s="%s"' % (prefix and ':%s' % prefix or '', ns)
        result = '<%s %s/>' % (elem.tag, namespaces)
        return StatusResponse(200, response.etag, result)

    def get_ns_bindings(self, uri, check_etag):
        d = self.get_document(uri, check_etag)
        return d.addCallbacks(self._cb_get_ns_bindings, callbackArgs=(uri, ))


class PresenceRulesApplication(ApplicationUsage):
    ## draft-ietf-simple-presence-rules-09
    id = "pres-rules"
    default_ns = "urn:ietf:params:xml:ns:pres-rules"
    mime_type = "application/auth-policy+xml"
    schema_file = 'common-policy.xsd'


class ResourceListsApplication(ApplicationUsage):
    ## RFC 4826
    id = "resource-lists"
    default_ns = "urn:ietf:params:xml:ns:resource-lists"
    mime_type= "application/resource-lists+xml"
    schema_file = 'resource-lists.xsd'

    @classmethod
    def check_lists(cls, elem, list_tag):
        """Check additional constraints (see section 3.4.5 of RFC 4826).

        elem is xml Element that containts <list>s
        list_tag is provided as argument since its namespace changes from resource-lists
        to rls-services namespace
        """
        entry_tag = "{%s}entry" % cls.default_ns
        entry_ref_tag = "{%s}entry-ref" % cls.default_ns
        external_tag ="{%s}tag" % cls.default_ns
        name_attrs = set()
        uri_attrs = set()
        ref_attrs = set()
        anchor_attrs = set()
        for child in elem.getchildren():
            if child.tag == list_tag:
                name = child.get("name")
                if name in name_attrs:
                    raise UniquenessFailureError()
                else:
                    name_attrs.add(name)
            elif child.tag == entry_tag:
                uri = child.get("uri")
                if uri in uri_attrs:
                    raise UniquenessFailureError()
                else:
                    uri_attrs.add(uri)
            elif child.tag == entry_ref_tag:
                ref = child.get("ref")
                if ref in ref_attrs:
                    raise UniquenessFailureError()
                else:
                    # TODO check if it's a relative URI, else raise ConstraintFailure
                    ref_attrs.add(ref)
            elif child.tag == external_tag:
                anchor = child.get("anchor")
                if anchor in anchor_attrs:
                    raise UniquenessFailureError()
                else:
                    # TODO check if it's a HTTP URL, else raise ConstraintFailure
                    anchor_attrs.add(anchor)

    def _check_additional_constraints(self, xml_doc):
        """Check additional constraints (see section 3.4.5 of RFC 4826)."""
        for elem in xml_doc.getiterator():
            self.check_lists(elem, "{%s}list" % self.default_ns)


class RLSServicesApplication(ApplicationUsage):
    ## RFC 4826
    id = "rls-services"
    default_ns = "urn:ietf:params:xml:ns:rls-services"
    mime_type= "application/rls-services+xml"
    schema_file = 'rls-services.xsd'

    def _check_additional_constraints(self, xml_doc):
        """Check additional constraints (see section 3.4.5 of RFC 4826)."""
        for elem in xml_doc.getiterator():
            ResourceListsApplication.check_lists(elem, "{%s}list" % self.default_ns)


class PIDFManipulationApplication(ApplicationUsage):
    ## RFC 4827
    id = "pidf-manipulation"
    default_ns = "urn:ietf:params:xml:ns:pidf"
    mime_type= "application/pidf+xml"
    schema_file = 'pidf.xsd'


class XCAPCapabilitiesApplication(ApplicationUsage):
    ## RFC 4825
    id = "xcap-caps"
    default_ns = "urn:ietf:params:xml:ns:xcap-caps"
    mime_type= "application/xcap-caps+xml"

    def __init__(self):
        pass

    def _get_document(self):
        if hasattr(self, 'doc'):
            return self.doc
        auids = ""
        extensions = ""
        namespaces = ""
        for (id, app) in applications.items():
            auids += "<auid>%s<auid>\n" % id
            namespaces += "<namespace>%s<namespace>\n" % app.default_ns
        self.doc = """<?xml version='1.0' encoding='UTF-8'?>
        <xcap-caps xmlns='urn:ietf:params:xml:ns:xcap-caps'>
            <auids>
            %(auids)s</auids>
            <extensions>
            %(extensions)s</extensions>
            <namespaces>
            %(namespaces)s</namespaces>
        </xcap-caps>""" % {"auids": auids,
                           "extensions": extensions,
                           "namespaces": namespaces}
        return self.doc

    def get_document(self, uri, check_etag):
        return defer.succeed(StatusResponse(200, data=self._get_document()))


class WatchersApplication(ResourceListsApplication):
    id = "watchers"
    default_ns = "http://openxcap.org/ns/watchers"
    mime_type= "application/xml"
    schema_file = 'watchers.xsd'

    def _watchers_to_xml(self, watchers):
        root = etree.Element("watchers", nsmap={None: self.default_ns})
        for watcher in watchers:
            watcher_elem = etree.SubElement(root, "watcher")
            for name, value in watcher.iteritems():
                element = etree.SubElement(watcher_elem, name)
                element.text = value
        doc = etree.tostring(root, encoding="utf-8", pretty_print=True, xml_declaration=True)
        #self.validate_document(doc)
        return StatusResponse(200, data=doc)

    def get_document(self, uri, check_etag):
        watchers_def = self.storage.get_watchers(uri)
        watchers_def.addCallback(self._watchers_to_xml)
        return watchers_def

    def put_document(self, uri, document, check_etag):
        raise ResourceNotFound("This application is read-only") # TODO: test and add better error

theStorage = ServerConfig.backend.Storage()

class TestApplication(ApplicationUsage):
    "Application for tests described in Section 8.2.3. Creation of RFC 4825"
    id = "test-app"
    default_ns = 'test-app'
    mime_type= "application/test-app+xml"
    schema_file = None

applications = {'xcap-caps': XCAPCapabilitiesApplication(),
                'pres-rules': PresenceRulesApplication(theStorage),
                'org.openmobilealliance.pres-rules': PresenceRulesApplication(theStorage),
                'resource-lists': ResourceListsApplication(theStorage),
                'pidf-manipulation': PIDFManipulationApplication(theStorage),
                'watchers': WatchersApplication(theStorage),
                'rls-services': RLSServicesApplication(theStorage),
                'test-app': TestApplication(theStorage)}

namespaces = dict((k, v.default_ns) for (k, v) in applications.items())

def getApplicationForURI(xcap_uri):
    return applications.get(xcap_uri.application_id, None)
