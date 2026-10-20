from setuptools import find_packages, setup


setup(
    name="energydesk-flexgateway-client",
    version="0.2.0",
    description="Reference client and contract test for the EnergyDesk Flexgateway API",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    python_requires=">=3.11",
    packages=find_packages(),
    install_requires=[
        "httpx>=0.27,<1",
        "python-dotenv>=1,<2",
    ],
    entry_points={
        "console_scripts": [
            "flexgateway-demo=energydesk_flexgateway_client.cli:main",
        ],
    },
)
