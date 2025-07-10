# Домашнее задание к занятию 6 «Создание собственных модулей»

## Подготовка к выполнению

1. Создайте пустой публичный репозиторий в своём любом проекте: `my_own_collection`.
2. Скачайте репозиторий Ansible: `git clone https://github.com/ansible/ansible.git` по любому, удобному вам пути.
3. Зайдите в директорию Ansible: `cd ansible`.
4. Создайте виртуальное окружение: `python3 -m venv venv`.
5. Активируйте виртуальное окружение: `. venv/bin/activate`. Дальнейшие действия производятся только в виртуальном окружении.
6. Установите зависимости `pip install -r requirements.txt`.
7. Запустите настройку окружения `. hacking/env-setup`.
8. Если все шаги прошли успешно — выйдите из виртуального окружения `deactivate`.
9. Ваше окружение настроено. Чтобы запустить его, нужно находиться в директории `ansible` и выполнить конструкцию `. venv/bin/activate && . hacking/env-setup`.
![image](https://github.com/user-attachments/assets/cffc8a7e-e694-402d-94e8-18535c91afb6)

## Основная часть

Ваша цель — написать собственный module, который вы можете использовать в своей role через playbook. Всё это должно быть собрано в виде collection и отправлено в ваш репозиторий.

**Шаг 1.** В виртуальном окружении создайте новый `my_own_module.py` файл.

**Шаг 2.** Наполните его содержимым:

```python
#!/usr/bin/python

# Copyright: (c) 2018, Terry Jones <terry.jones@example.org>
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

DOCUMENTATION = r'''
---
module: my_test

short_description: This is my test module

# If this is part of a collection, you need to use semantic versioning,
# i.e. the version is of the form "2.5.0" and not "2.4".
version_added: "1.0.0"

description: This is my longer description explaining my test module.

options:
    name:
        description: This is the message to send to the test module.
        required: true
        type: str
    new:
        description:
            - Control to demo if the result of this module is changed or not.
            - Parameter description can be a list as well.
        required: false
        type: bool
# Specify this value according to your collection
# in format of namespace.collection.doc_fragment_name
extends_documentation_fragment:
    - my_namespace.my_collection.my_doc_fragment_name

author:
    - Your Name (@yourGitHubHandle)
'''

EXAMPLES = r'''
# Pass in a message
- name: Test with a message
  my_namespace.my_collection.my_test:
    name: hello world

# pass in a message and have changed true
- name: Test with a message and changed output
  my_namespace.my_collection.my_test:
    name: hello world
    new: true

# fail the module
- name: Test failure of the module
  my_namespace.my_collection.my_test:
    name: fail me
'''

RETURN = r'''
# These are examples of possible return values, and in general should use other names for return values.
original_message:
    description: The original name param that was passed in.
    type: str
    returned: always
    sample: 'hello world'
message:
    description: The output message that the test module generates.
    type: str
    returned: always
    sample: 'goodbye'
'''

from ansible.module_utils.basic import AnsibleModule


def run_module():
    # define available arguments/parameters a user can pass to the module
    module_args = dict(
        name=dict(type='str', required=True),
        new=dict(type='bool', required=False, default=False)
    )

    # seed the result dict in the object
    # we primarily care about changed and state
    # changed is if this module effectively modified the target
    # state will include any data that you want your module to pass back
    # for consumption, for example, in a subsequent task
    result = dict(
        changed=False,
        original_message='',
        message=''
    )

    # the AnsibleModule object will be our abstraction working with Ansible
    # this includes instantiation, a couple of common attr would be the
    # args/params passed to the execution, as well as if the module
    # supports check mode
    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    # if the user is working with this module in only check mode we do not
    # want to make any changes to the environment, just return the current
    # state with no modifications
    if module.check_mode:
        module.exit_json(**result)

    # manipulate or modify the state as needed (this is going to be the
    # part where your module will do what it needs to do)
    result['original_message'] = module.params['name']
    result['message'] = 'goodbye'

    # use whatever logic you need to determine whether or not this module
    # made any modifications to your target
    if module.params['new']:
        result['changed'] = True

    # during the execution of the module, if there is an exception or a
    # conditional state that effectively causes a failure, run
    # AnsibleModule.fail_json() to pass in the message and the result
    if module.params['name'] == 'fail me':
        module.fail_json(msg='You requested this to fail', **result)

    # in the event of a successful module execution, you will want to
    # simple AnsibleModule.exit_json(), passing the key/value results
    module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()
```
Или возьмите это наполнение [из статьи](https://docs.ansible.com/ansible/latest/dev_guide/developing_modules_general.html#creating-a-module).

**Шаг 3.** Заполните файл в соответствии с требованиями Ansible так, чтобы он выполнял основную задачу: module должен создавать текстовый файл на удалённом хосте по пути, определённом в параметре `path`, с содержимым, определённым в параметре `content`.

**Шаг 4.** Проверьте module на исполняемость локально.

Файл создается 

Тело скрипта my_own_module.py:

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
    result = dict(
        file_created=True
    )
    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )
    if module.check_mode:
        module.exit_json(**result)
    try:
        file_out = open( module.params['path'], "w", encoding="utf-8" )
        file_out.write( module.params['content'] )
        file_out.close
    except:
        result['file_created'] = False
        module.fail_json( msg='File creation error!', **result )
    module.exit_json(**result)


def main():
    file_create_module()

if __name__ == '__main__':
    main()

Тело скрипта file.json:

{
    "ANSIBLE_MODULE_ARGS": {
        "path": "/tmp/test.txt",
        "content": "test"
    }
}

![image](https://github.com/user-attachments/assets/84e4305a-22b4-4e70-b6c6-18a7e9d3ef1e)



**Шаг 5.** Напишите single task playbook и используйте module в нём.

single_task_playbook.yml

![image](https://github.com/user-attachments/assets/2e4d6465-bd63-46e4-ab95-eb859d3ffac5)


---
- name: Test my_own_module
  hosts: localhost
  tasks:
  - name: Execute module
    my_own_module:
      path: "/tmp/test02.txt"
      content: "Test02"




**Шаг 6.** Проверьте через playbook на идемпотентность.

![image](https://github.com/user-attachments/assets/828a66d7-1a65-4b24-bc40-4581d8a4231b)



**Шаг 7.** Выйдите из виртуального окружения.

**Шаг 8.** Инициализируйте новую collection: `ansible-galaxy collection init my_own_namespace.yandex_cloud_elk`.


![image](https://github.com/user-attachments/assets/a7fc5a52-dddd-4343-bfa2-0a9edaaa1bb4)


**Шаг 9.** В эту collection перенесите свой module в соответствующую директорию.

cp ./lib/ansible/modules/my_own_module.py ./my_own_namespace/yandex_cloud_elk/plugins/modules/

![image](https://github.com/user-attachments/assets/4e721634-3ae6-488d-b31b-0878220dfa8f)



**Шаг 10.** Single task playbook преобразуйте в single task role и перенесите в collection. У role должны быть default всех параметров module.

**Шаг 11.** Создайте playbook для использования этой role.

site.yml:
---
- name: Test my_own_module in yandex.cloud
  hosts:
    - localhost
#    - ubuntu-ansible
  roles:
    - my_role

    
![image](https://github.com/user-attachments/assets/d5b347fa-a574-46fe-b038-2ef538584adf)



**Шаг 12.** Заполните всю документацию по collection, выложите в свой репозиторий, поставьте тег `1.0.0` на этот коммит.

**Шаг 13.** Создайте .tar.gz этой collection: `ansible-galaxy collection build` в корневой директории collection.

![image](https://github.com/user-attachments/assets/9d8c1d89-d78a-4f8e-96a3-247941c29c5f)



**Шаг 14.** Создайте ещё одну директорию любого наименования, перенесите туда single task playbook и архив c collection.

**Шаг 15.** Установите collection из локального архива: `ansible-galaxy collection install <archivename>.tar.gz`.

![image](https://github.com/user-attachments/assets/29251a9b-275e-415b-8ea2-00be5bea0dba)





**Шаг 16.** Запустите playbook, убедитесь, что он работает.

![image](https://github.com/user-attachments/assets/a4906d44-11be-4f2b-8f4b-fd877ce18d34)



**Шаг 17.** В ответ необходимо прислать ссылки на collection и tar.gz архив, а также скриншоты выполнения пунктов 4, 6, 15 и 16.

https://github.com/olegveselov1984/my_own_collection/releases/tag/v1.0.0

https://github.com/olegveselov1984/my_own_collection/blob/a8c63da9dc5975378d37fbf368cdb6f0629e2da4/ansible/my_own_namespace/yandex_cloud_elk/my_own_namespace-yandex_cloud_elk-1.0.0.tar.gz



## Необязательная часть

1. Реализуйте свой модуль для создания хостов в Yandex Cloud.
2. Модуль может и должен иметь зависимость от `yc`, основной функционал: создание ВМ с нужным сайзингом на основе нужной ОС. Дополнительные модули по созданию кластеров ClickHouse, MySQL и прочего реализовывать не надо, достаточно простейшего создания ВМ.
3. Модуль может формировать динамическое inventory, но эта часть не является обязательной, достаточно, чтобы он делал хосты с указанной спецификацией в YAML.
4. Протестируйте модуль на идемпотентность, исполнимость. При успехе добавьте этот модуль в свою коллекцию.
5. Измените playbook так, чтобы он умел создавать инфраструктуру под inventory, а после устанавливал весь ваш стек Observability на нужные хосты и настраивал его.
6. В итоге ваша коллекция обязательно должна содержать: clickhouse-role (если есть своя), lighthouse-role, vector-role, два модуля: my_own_module и модуль управления Yandex Cloud хостами и playbook, который демонстрирует создание Observability стека.

---

### Как оформить решение задания

Выполненное домашнее задание пришлите в виде ссылки на .md-файл в вашем репозитории.

---
