from setuptools import setup, find_packages

setup(
    name="genworlds",
    version="0.0.1",
    description="GenWorlds by YeagerAI: Pioneering AI-based simulations based on collaborative autonomous agents.",
    author="YeagerAI LLC",
    author_email="jm@yeager.ai",
    packages=["genworlds"],
    install_requires=[
        "chromadb==0.3.21",
        "click==8.1.3",
        "colorama==0.4.6",
        "colorlog==6.7.0",
        "faiss-cpu==1.7.3",
        "fastapi==0.88.0",
        "fastjsonschema==2.16.3",
        "jsonschema==4.17.3",
        "langchain==0.0.157",
        "openai==0.27.4",
        "pydantic==1.10.7",
        "python-dotenv==1.0.0",
        "threadpoolctl==3.1.0",
        "tiktoken==0.4.0",
        "uvicorn==0.21.1",
        "websocket-client==1.5.1",
        "websockets==11.0.3",
    ],
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "Environment :: Web Environment",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)