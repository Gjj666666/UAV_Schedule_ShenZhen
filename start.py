#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键启动深圳低空多无人机调度 Demo。"""
import uvicorn
import os


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=False)
