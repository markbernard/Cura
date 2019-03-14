# Copyright (c) 2019 Ultimaker B.V.
# Cura is released under the terms of the LGPLv3 or higher.

from collections import deque
from threading import RLock
from typing import Callable, Dict, Set, Union, Optional, TYPE_CHECKING

from PyQt5.QtCore import QObject, QUrl
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest

from UM.Logger import Logger

if TYPE_CHECKING:
    from PyQt5.QtNetwork import QNetworkReply


#
# This is an internal data class which holds all data regarding a network request.
#  - http_method: The HTTP method to use for this request, e.g. GET, PUT, POST, etc.
#  - request: The QNetworkRequest object that's created for this request
#  - data (optional): The data in binary form that needs to be sent.
#  - callback (optional): The callback function that will be triggered when the request is finished.
#  - error_callback (optional): The callback function for handling errors.
#  - download_progress_callback (optional): The callback function for handling download progress.
#  - upload_progress_callback (optional): The callback function for handling upload progress.
#  - reply: The QNetworkReply for this request. It will only present after this request gets processed.
#
class HttpNetworkRequestData:
    def __init__(self, http_method: str, request: "QNetworkRequest",
                 data: Optional[Union[bytes, bytearray]] = None,
                 callback: Optional[Callable[["QNetworkReply"], None]] = None,
                 error_callback: Optional[Callable[["QNetworkReply", "QNetworkReply.NetworkError"], None]] = None,
                 download_progress_callback: Optional[Callable[[int, int], None]] = None,
                 upload_progress_callback: Optional[Callable[[int, int], None]] = None,
                 reply: Optional["QNetworkReply"] = None) -> None:
        self.http_method = http_method.lower()
        self.request = request
        self.data = data
        self.callback = callback
        self.error_callback = error_callback
        self.download_progress_callback = download_progress_callback
        self.upload_progress_callback = upload_progress_callback
        self.reply = reply

    def __str__(self) -> str:
        data = "no-data"
        if self.data:
            data = self.data[:10]
            if len(self.data) > 10:
                data += "..."

        return "request[{method}][{url}][{data}]".format(method = self.http_method,
                                                         url = self.request.url(),
                                                         data = data)


#
# A dedicated manager that processes and schedules HTTP requests. It provides public APIs for issuing HTTP requests
# and the results, successful or not, will be communicated back via callback functions. For each request, 2 callback
# functions can be optionally specified:
#
#  - callback: This function will be invoked when a request finishes. (bound to QNetworkReply.finished signal)
#              Its signature should be "def callback(QNetworkReply) -> None" or in any other compatible form.
#
#  - error_callback: This function will be invoked when a request fails. (bound to QNetworkReply.error signal)
#                    Its signature should be "def callback(QNetworkReply, QNetworkReply.NetworkError) -> None"
#                    or in any other compatible form.
#
class HttpNetworkRequestManager(QObject):

    def __init__(self, max_concurrent_requests: int = 10, parent: Optional["QObject"] = None) -> None:
        super().__init__(parent)

        from cura.CuraApplication import CuraApplication
        self._application = CuraApplication.getInstance()

        self._network_manager = QNetworkAccessManager(self)

        # Max number of concurrent requests that can be issued
        self._max_concurrent_requests = max_concurrent_requests
        self._request_queue = deque()

        self._current_requests = set()  # type: Set[HttpNetworkRequestData]
        self._request_lock = RLock()
        self._process_requests_scheduled = False

    # Public API for creating an HTTP GET request.
    def get(self, url: str,
            headers_dict: Optional[Dict[str, str]] = None,
            callback: Optional[Callable[["QNetworkReply"], None]] = None,
            error_callback: Optional[Callable[["QNetworkReply", "QNetworkReply.NetworkError"], None]] = None,
            download_progress_callback: Optional[Callable[[int, int], None]] = None,
            upload_progress_callback: Optional[Callable[[int, int], None]] = None) -> None:
        self._createRequest("get", url, headers_dict = headers_dict,
                            callback = callback, error_callback = error_callback,
                            download_progress_callback = download_progress_callback,
                            upload_progress_callback = upload_progress_callback)

    # Public API for creating an HTTP PUT request.
    def put(self, url: str,
            headers_dict: Optional[Dict[str, str]] = None,
            data: Optional[Union[bytes, bytearray]] = None,
            callback: Optional[Callable[["QNetworkReply"], None]] = None,
            error_callback: Optional[Callable[["QNetworkReply", "QNetworkReply.NetworkError"], None]] = None,
            download_progress_callback: Optional[Callable[[int, int], None]] = None,
            upload_progress_callback: Optional[Callable[[int, int], None]] = None) -> None:
        self._createRequest("put", url, headers_dict = headers_dict, data = data,
                            callback = callback, error_callback = error_callback,
                            download_progress_callback = download_progress_callback,
                            upload_progress_callback = upload_progress_callback)

    # Public API for creating an HTTP POST request.
    def post(self, url: str,
             headers_dict: Optional[Dict[str, str]] = None,
             data: Optional[Union[bytes, bytearray]] = None,
             callback: Optional[Callable[["QNetworkReply"], None]] = None,
             error_callback: Optional[Callable[["QNetworkReply", "QNetworkReply.NetworkError"], None]] = None,
             download_progress_callback: Optional[Callable[[int, int], None]] = None,
             upload_progress_callback: Optional[Callable[[int, int], None]] = None) -> None:
        self._createRequest("post", url, headers_dict = headers_dict, data = data,
                            callback = callback, error_callback = error_callback,
                            download_progress_callback = download_progress_callback,
                            upload_progress_callback = upload_progress_callback)

    # This function creates a HttpNetworkRequestData with the given data and puts it into the pending request queue.
    # If no request processing call has been scheduled, it will schedule it too.
    def _createRequest(self, http_method: str, url: str,
                       headers_dict: Optional[Dict[str, str]] = None,
                       data: Optional[Union[bytes, bytearray]] = None,
                       callback: Optional[Callable[["QNetworkReply"], None]] = None,
                       error_callback: Optional[Callable[["QNetworkReply", "QNetworkReply.NetworkError"], None]] = None,
                       download_progress_callback: Optional[Callable[[int, int], None]] = None,
                       upload_progress_callback: Optional[Callable[[int, int], None]] = None) -> None:
        request = QNetworkRequest(QUrl(url))

        # Make sure that Qt handles redirects
        if hasattr(QNetworkRequest, "FollowRedirectsAttribute"):
            # Patch for Qt 5.6-5.8
            request.setAttribute(QNetworkRequest.FollowRedirectsAttribute, True)
        if hasattr(QNetworkRequest, "RedirectPolicyAttribute"):
            # Patch for Qt 5.9+
            request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, True)

        # Set headers
        if headers_dict is not None:
            for key, value in headers_dict.items():
                if isinstance(key, str):
                    key = key.encode("utf-8")
                if isinstance(value, str):
                    value = value.encode("utf-8")
                request.setRawHeader(key, value)

        # Create the request data
        request_data = HttpNetworkRequestData(http_method = http_method,
                                              request = request,
                                              data = data,
                                              callback = callback,
                                              error_callback = error_callback,
                                              download_progress_callback = download_progress_callback,
                                              upload_progress_callback = upload_progress_callback)

        with self._request_lock:
            Logger.log("d", "%s has been queued", request_data)
            self._request_queue.append(request_data)

            # Schedule a call to process pending requests in the queue
            if not self._process_requests_scheduled:
                self._application.callLater(self._processRequestsInQueue)
                self._process_requests_scheduled = True
                Logger.log("d", "process requests call has been scheduled")

    # Processes the next request in the pending queue. Stops if there is no more pending requests. It also stops if
    # the maximum number of concurrent requests has been reached.
    def _processRequestsInQueue(self) -> None:
        with self._request_lock:
            # do nothing if there's no more requests to process
            if not self._request_queue:
                self._process_requests_scheduled = False
                Logger.log("d", "No more requests to process, stop")
                return

            # do not exceed the max request limit
            if len(self._current_requests) >= self._max_concurrent_requests:
                self._process_requests_scheduled = False
                Logger.log("d", "The in-progress requests has reached the limit %s, stop",
                           self._max_concurrent_requests)
                return

            # fetch the next request and process
            next_request_data = self._request_queue.popleft()
        self._processRequest(next_request_data)

    # Processes the given HttpNetworkRequestData by issuing the request using QNetworkAccessManager and moves the
    # request into the currently in-progress list.
    def _processRequest(self, request_data: "HttpNetworkRequestData") -> None:
        Logger.log("d", "Start processing %s", request_data)

        # get the right http_method function and prepare arguments.
        method = getattr(self._network_manager, request_data.http_method)
        args = [request_data.request]
        if request_data.data is not None:
            args.append(request_data.data)

        # issue the request and add the reply into the currently in-progress requests set
        reply = method(*args)
        request_data.reply = reply

        # connect callback signals
        reply.error.connect(lambda err, rd = request_data: self._onRequestError(rd, err))
        reply.finished.connect(lambda rd = request_data: self._onRequestFinished(rd))
        if request_data.download_progress_callback is not None:
            reply.downloadProgress.connect(request_data.download_progress_callback)
        if request_data.upload_progress_callback is not None:
            reply.uploadProgress.connect(request_data.upload_progress_callback)

        with self._request_lock:
            self._current_requests.add(request_data)

    def _onRequestError(self, request_data: "HttpNetworkRequestData", error: "QNetworkReply.NetworkError") -> None:
        with self._request_lock:
            # safeguard: make sure that we have the reply in the currently in-progress requests set
            if request_data not in self._current_requests:
                # TODO: ERROR, should not happen
                Logger.log("e", "%s not found in the in-progress set", request_data)
                pass

            # disconnect callback signals
            if request_data.download_progress_callback is not None:
                request_data.reply.downloadProgress.disconnect()
            if request_data.upload_progress_callback is not None:
                request_data.reply.uploadProgress.disconnect()

            self._current_requests.remove(request_data)

        Logger.log("d", "%s got error", request_data)

        # schedule the error callback if there is one
        if request_data.error_callback is not None:
            Logger.log("d", "%s error callback scheduled", request_data)
            self._application.callLater(request_data.error_callback, request_data.reply, error)

        # continue to process the next request
        self._processRequestsInQueue()

    def _onRequestFinished(self, request_data: "HttpNetworkRequestData") -> None:
        with self._request_lock:
            # safeguard: make sure that we have the reply in the currently in-progress requests set
            if request_data not in self._current_requests:
                # TODO: ERROR, should not happen
                Logger.log("e", "%s not found in the in-progress set", request_data)
                pass

            # disconnect callback signals
            if request_data.download_progress_callback is not None:
                request_data.reply.downloadProgress.disconnect()
            if request_data.upload_progress_callback is not None:
                request_data.reply.uploadProgress.disconnect()

            self._current_requests.remove(request_data)

        Logger.log("d", "%s finished", request_data)

        # schedule the callback if there is one
        if request_data.callback is not None:
            Logger.log("d", "%s callback scheduled", request_data)
            self._application.callLater(request_data.callback, request_data.reply)

        # continue to process the next request
        self._processRequestsInQueue()


__all__ = ["HttpNetworkRequestManager"]
