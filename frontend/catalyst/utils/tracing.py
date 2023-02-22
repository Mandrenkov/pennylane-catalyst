# Copyright 2022-2023 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


class TracingContext:
    _is_tracing = False

    def __enter__(self):
        assert not TracingContext._is_tracing, "Cannot nest tracing contexts."
        TracingContext._is_tracing = True

    def __exit__(self, *args, **kwargs):
        TracingContext._is_tracing = False

    @staticmethod
    def is_tracing():
        return TracingContext._is_tracing

    @staticmethod
    def check_is_tracing(msg):
        if not TracingContext.is_tracing():
            raise RuntimeError(msg)