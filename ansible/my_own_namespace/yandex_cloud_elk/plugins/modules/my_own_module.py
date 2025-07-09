#!/usr/bin/python

from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

import os.path

DOCUMENTATION = r'''
---
module: file_creator

short_description: Создание текстового файла

version_added: "1.0.0"

description: Создание текстового файла, путь и содержимое.

options:
    path:
        description: Полный путь к файлу включая имя и расширение
        required: true
        type: str
    content:
        description: Текстовое содержимое файла
        required: true
        type: str

extends_documentation_fragment:
    - HW01.HW01_1.file_name

author:
    - olegveselov1984 (@olegveselov1984)
'''

EXAMPLES = r'''
- name: Test
  HW01.HW01_1.file_name:
    path: /tmp/filename.txt
    content: test
'''

RETURN = r'''
#{"file_created": true, "invocation": {"module_args": {"path": "/tmp/filename.txt", "content": "test"}}}
'''

from ansible.module_utils.basic import AnsibleModule

def file_create_module():

    module_args = dict(
        path = dict( type = 'str', required = True ),
        content = dict( type = 'str', required = True )
    )

    # result = dict(
    #     file_created=True
    # )
    
    result = dict(
        changed=False,
        original_message='',
        message=''
    )


    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )


##############Проверка №1

    if module.check_mode:
        module.exit_json(**result)

    if os.path.exists(module.params['path']):
        result['changed'] = False
        result['message'] = 'File already exist'
    else:
        target_file = open(module.params['path'], "w")
        target_file.write(module.params['content'])
        target_file.close()
        result['changed'] = True
        result['message'] = 'File was created!'

    result['original_message'] = module.params['content']

    module.exit_json(**result)



##############Проверка №2


    # try:
    #     file_out = open( module.params['path'], "w", encoding="utf-8" )
    #     file_out.write( module.params['content'] )
    #     file_out.close
    # except:
    #     result['file_created'] = False
    #     module.fail_json( msg='File creation error!', **result )
    # module.exit_json(**result)

def main():
    file_create_module()

if __name__ == '__main__':
    main()