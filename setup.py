from setuptools import setup

setup(
    name='HiveMind-PtT',
    version='0.1.2',
    packages=['mycroft_ptt', 'mycroft_ptt.speech',
              'mycroft_ptt.res',
              'mycroft_ptt.configuration'],
    install_requires=["jarbas_hive_mind>=0.10.3",
                      "speech2text",
                      "pyee",
                      "requests",
                      "requests_futures",
                      "psutil",
                      "PyAudio==0.2.11",
                      "jarbas_utils",
                      "text2speech"],
    include_package_data=True,
    url='https://github.com/OpenJarbas/HiveMind-voice-sat',
    license='MIT',
    author='jarbasAI',
    author_email='jarbasai@mailfence.com',
    description='Mycroft Push to Talk Satellite',
    entry_points={
        'console_scripts': [
            'HiveMind-ptt=mycroft_ptt.__main__:main'
        ]
    }
)
