# VMware vCloud Director Python SDK
# Copyright (c) 2014 VMware, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from lxml import etree
from lxml import objectify
import os
from pyvcloud.vcd.client import _TaskMonitor
from pyvcloud.vcd.client import EntityType
from pyvcloud.vcd.client import find_link
from pyvcloud.vcd.client import get_links
from pyvcloud.vcd.client import QueryResultFormat
from pyvcloud.vcd.client import RelationType
from pyvcloud.vcd.utils import to_dict
import shutil
import tarfile
import tempfile
import time
import traceback


Maker = objectify.ElementMaker(
    annotate=False,
    namespace='',
    nsmap={None: 'http://www.vmware.com/vcloud/v1.5',
           'ovf': 'http://schemas.dmtf.org/ovf/envelope/1'})


DEFAULT_CHUNK_SIZE = 1024*1024


# TODO(cache org_resource)

class Org(object):

    def __init__(self,
                 client,
                 org_href=None,
                 is_admin=False,
                 org_resource=None):
        self.client = client
        self.endpoint = org_href
        self.org_resource = org_resource
        if org_resource is not None:
            self.endpoint = org_resource.get('href')
        self.endpoint_admin = self.endpoint.replace('/api/org/',
                                                    '/api/admin/org/')
        self.is_admin = is_admin

    def create_catalog(self, name, description):
        catalog = Maker.AdminCatalog(name=name)
        catalog.append(Maker.Description(description))
        return self.client.post_resource(
            self.endpoint_admin + '/catalogs',
            catalog,
            EntityType.ADMIN_CATALOG.value)

    def delete_catalog(self, name):
        org = self.client.get_resource(self.endpoint)
        links = get_links(org,
                          rel=RelationType.DOWN,
                          media_type=EntityType.CATALOG.value)
        for link in links:
            if name == link.name:
                admin_href = link.href.replace('/api/catalog/',
                                               '/api/admin/catalog/')
                return self.client.delete_resource(admin_href)
        raise Exception('Catalog not found.')

    def list_catalogs(self):
        if self.is_admin:
            resource_type = 'adminCatalog'
        else:
            resource_type = 'catalog'
        result = []
        q = self.client.get_typed_query(
            resource_type,
            query_result_format=QueryResultFormat.ID_RECORDS)
        records = list(q.execute())
        if len(records) == 0:
            result = 'No catalogs found.'
        else:
            for r in records:
                result.append(to_dict(r,
                                      resource_type=resource_type,
                                      exclude=['owner', 'org']))
        return result

    def get_catalog(self, name):
        org = self.client.get_resource(self.endpoint)
        links = get_links(org,
                          rel=RelationType.DOWN,
                          media_type=EntityType.CATALOG.value)
        for link in links:
            if name == link.name:
                return self.client.get_resource(link.href)
        raise Exception('Catalog not found.')

    def share_catalog(self, name, share=True):
        catalog = self.get_catalog(name)
        is_published = 'true' if share else 'false'
        params = Maker.PublishCatalogParams(Maker.IsPublished(is_published))
        href = catalog.get('href') + '/action/publish'
        admin_href = href.replace('/api/catalog/', '/api/admin/catalog/')
        return self.client.post_resource(
            admin_href,
            params,
            media_type=EntityType.PUBLISH_CATALOG_PARAMS.value
            )

    def list_catalog_items(self, name):
        catalog = self.get_catalog(name)
        items = []
        for i in catalog.CatalogItems.getchildren():
            items.append({'name': i.get('name'), 'id': i.get('id')})
        return items

    def get_catalog_item(self, name, item_name):
        catalog = self.get_catalog(name)
        for i in catalog.CatalogItems.getchildren():
            if i.get('name') == item_name:
                return self.client.get_resource(i.get('href'))
        raise Exception('Catalog item not found.')

    def delete_catalog_item(self, name, item_name):
        catalog = self.get_catalog(name)
        for i in catalog.CatalogItems.getchildren():
            if i.get('name') == item_name:
                return self.client.delete_resource(i.get('href'))
        raise Exception('Item not found.')

    def upload_media(self,
                     catalog_name,
                     file_name,
                     item_name=None,
                     description='',
                     chunk_size=DEFAULT_CHUNK_SIZE,
                     callback=None):
        stat_info = os.stat(file_name)
        catalog = self.get_catalog(catalog_name)
        if item_name is None:
            item_name = os.path.basename(file_name)
        image_type = os.path.splitext(item_name)[1][1:]
        media = Maker.Media(name=item_name,
                            size=str(stat_info.st_size),
                            imageType=image_type)
        media.append(Maker.Description(description))
        catalog_item = self.client.post_resource(
            catalog.get('href') + '/action/upload',
            media,
            EntityType.MEDIA.value)
        entity = self.client.get_resource(catalog_item.Entity.get('href'))
        file_href = entity.Files.File.Link.get('href')
        return self.upload_file(file_name, file_href, chunk_size=chunk_size,
                                callback=callback)

    def download_catalog_item(self,
                              catalog_name,
                              item_name,
                              file_name,
                              chunk_size=DEFAULT_CHUNK_SIZE,
                              callback=None,
                              task_callback=None):
        item = self.get_catalog_item(catalog_name, item_name)
        item_type = item.Entity.get('type')
        enable_href = item.Entity.get('href') + '/action/enableDownload'
        task = self.client.post_resource(enable_href, None, None)
        tm = _TaskMonitor(self.client)
        tm.wait_for_success(task, 60, 1, callback=task_callback)
        item = self.client.get_resource(item.Entity.get('href'))
        bytes_written = 0
        if item_type == EntityType.MEDIA.value:
            size = item.Files.File.get('size')
            download_href = item.Files.File.Link.get('href')
            bytes_written = self.client.download_from_uri(
                download_href,
                file_name,
                chunk_size=chunk_size,
                size=size,
                callback=callback)
        elif item_type == EntityType.VAPP_TEMPLATE.value:
            ovf_descriptor = self.client.get_linked_resource(
                item,
                RelationType.DOWNLOAD_DEFAULT,
                EntityType.TEXT_XML.value)
            transfer_uri = find_link(item,
                                     RelationType.DOWNLOAD_DEFAULT,
                                     EntityType.TEXT_XML.value).href
            transfer_uri = transfer_uri.replace('/descriptor.ovf', '/')
            tempdir = None
            cwd = os.getcwd()
            try:
                tempdir = tempfile.mkdtemp(dir='.')
                ovf_file = os.path.join(tempdir, 'descriptor.ovf')
                with open(ovf_file, 'wb') as f:
                    payload = etree.tostring(ovf_descriptor,
                                             pretty_print=True,
                                             xml_declaration=True,
                                             encoding='utf-8')
                    f.write(payload)

                ns = '{http://schemas.dmtf.org/ovf/envelope/1}'
                files = []
                for f in ovf_descriptor.References.File:
                    source_file = {
                        'href': f.get(ns + 'href'),
                        'name': f.get(ns + 'id'),
                        'size': f.get(ns + 'size')
                        }
                    target_file = os.path.join(tempdir, source_file['href'])
                    uri = transfer_uri + source_file['href']
                    num_bytes = self.client.download_from_uri(
                        uri,
                        target_file,
                        chunk_size=chunk_size,
                        size=source_file['size'],
                        callback=callback)
                    if num_bytes != source_file['size']:
                        raise Exception('download incomplete for file %s' %
                                        source_file['href'])
                    files.append(source_file)
                with tarfile.open(file_name, 'w') as tar:
                    os.chdir(tempdir)
                    tar.add('descriptor.ovf')
                    for f in files:
                        tar.add(f['href'])
            finally:
                if tempdir is not None:
                    os.chdir(cwd)
                    stat_info = os.stat(file_name)
                    bytes_written = stat_info.st_size
                    # shutil.rmtree(tempdir)
        return bytes_written

    def upload_file(self,
                    file_name,
                    href,
                    chunk_size=DEFAULT_CHUNK_SIZE,
                    callback=None):
        transferred = 0
        stat_info = os.stat(file_name)
        with open(file_name, 'rb') as f:
            while transferred < stat_info.st_size:
                my_bytes = f.read(chunk_size)
                if len(my_bytes) <= chunk_size:
                    range_str = 'bytes %s-%s/%s' % \
                                (transferred,
                                 len(my_bytes)-1,
                                 stat_info.st_size)
                    self.client.upload_fragment(href, my_bytes, range_str)
                    transferred += len(my_bytes)
                    if callback is not None:
                        callback(transferred, stat_info.st_size)
        return transferred

    def upload_ovf(self,
                   catalog_name,
                   file_name,
                   item_name=None,
                   description='',
                   chunk_size=DEFAULT_CHUNK_SIZE,
                   callback=None):
        catalog = self.get_catalog(catalog_name)
        if item_name is None:
            item_name = os.path.basename(file_name)
        tempdir = tempfile.mkdtemp(dir='.')
        total_bytes = 0
        try:
            ova = tarfile.open(file_name)
            ova.extractall(path=tempdir)
            ova.close()
            ovf_file = None
            files = os.listdir(tempdir)
            for f in files:
                fn, ex = os.path.splitext(f)
                if ex == '.ovf':
                    ovf_file = os.path.join(tempdir, f)
                    break
            if ovf_file is not None:
                stat_info = os.stat(ovf_file)
                total_bytes += stat_info.st_size
                ovf = objectify.parse(ovf_file)
                files = []
                ns = '{http://schemas.dmtf.org/ovf/envelope/1}'
                for f in ovf.getroot().References.File:
                    source_file = {
                        'href': f.get(ns + 'href'),
                        'name': f.get(ns + 'id'),
                        'size': f.get(ns + 'size')
                        }
                    files.append(source_file)
                if item_name is None:
                    item_name = os.path.basename(file_name)
                params = Maker.UploadVAppTemplateParams(name=item_name)
                params.append(Maker.Description(description))
                catalog_item = self.client.post_resource(
                    catalog.get('href') + '/action/upload',
                    params,
                    EntityType.UPLOAD_VAPP_TEMPLATE_PARAMS.value)
                entity = self.client.get_resource(catalog_item.
                                                  Entity.get('href'))
                file_href = entity.Files.File.Link.get('href')
                self.client.put_resource(file_href, ovf, 'text/xml')
                while True:
                    time.sleep(5)
                    entity = self.client.get_resource(catalog_item.
                                                      Entity.get('href'))
                    if len(entity.Files.File) > 1:
                        break
                for source_file in files:
                    for target_file in entity.Files.File:
                        if source_file.get('href') == target_file.get('name'):
                            file_path = os.path.join(tempdir,
                                                     source_file.get('href'))
                            total_bytes += self.upload_file(
                                file_path,
                                target_file.Link.get('href'),
                                chunk_size=chunk_size,
                                callback=callback)
            shutil.rmtree(tempdir)
        except Exception as e:
            print(traceback.format_exc())
            shutil.rmtree(tempdir)
            raise e
        return total_bytes

    def get_vdc(self, name):
        if self.org_resource is None:
            self.org_resource = self.client.get_resource(self.endpoint)
        links = get_links(self.org_resource,
                          rel=RelationType.DOWN,
                          media_type=EntityType.VDC.value)
        for link in links:
            if name == link.name:
                return self.client.get_resource(link.href)
