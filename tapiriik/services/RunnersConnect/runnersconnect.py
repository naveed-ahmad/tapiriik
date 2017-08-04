from tapiriik.services.service_base import ServiceBase
from tapiriik.services.interchange import UploadedActivity, ActivityType, ActivityStatistic, ActivityStatisticUnit, Waypoint, WaypointType, Location, Lap
from tapiriik.services.api import APIException, APIExcludeActivity, UserException, UserExceptionType
from tapiriik.database import redis

from django.core.urlresolvers import reverse
from datetime import timedelta, datetime
import dateutil.parser
import requests
import logging
import pytz
import json
import os
import hashlib

logger = logging.getLogger(__name__)


class RunnersConnectService(ServiceBase):
    ReceivesActivities = True
    ID = "runnersconnect"
    DisplayName = "RunnersConnect"
    DisplayAbbreviation = "RC"
    UPLOAD_ACTIVITY_URL = "http://staging.runnersconnect.net/services/upload_activity"
    UserProfileURL = "https://app.runnersconnect/profiles/{0}"
    UserActivityURL = "http://app.runnersconnect/external_activities/{1}/{0}"
    RequiresExtendedAuthorizationDetails = True
    PartialSyncTriggerRequiresPolling = False
    ReceivesStationaryActivities = True
    UploadRetryCount = 10

    # The complete list:
    # running,cycling transportation,cycling sport,mountain biking,skating,roller skiing,skiing cross country,skiing downhill,snowboarding,kayaking,kite surfing,rowing,sailing,windsurfing,fitness walking,golfing,hiking,orienteering,walking,riding,swimming,spinning,other,aerobics,badminton,baseball,basketball,boxing,stair climbing,cricket,cross training,dancing,fencing,american football,rugby,soccer,handball,hockey,pilates,polo,scuba diving,squash,table tennis,tennis,beach volley,volleyball,weight training,yoga,martial arts,gymnastics,step counter,crossfit,treadmill running,skateboarding,surfing,snowshoeing,wheelchair,climbing,treadmill walking
    _activityMappings = {
        "running": ActivityType.Running,
        "cycling transportation": ActivityType.Cycling,
        "cycling sport": ActivityType.Cycling,
        "mountain biking": ActivityType.Cycling,
        "skating": ActivityType.Skating,
        "skiing cross country": ActivityType.CrossCountrySkiing,
        "skiing downhill": ActivityType.DownhillSkiing,
        "snowboarding": ActivityType.Snowboarding,
        "rowing": ActivityType.Rowing,
        "fitness walking": ActivityType.Walking,
        "hiking": ActivityType.Hiking,
        "orienteering": ActivityType.Walking,
        "walking": ActivityType.Walking,
        "swimming": ActivityType.Swimming,
        "other": ActivityType.Other,
        "treadmill running": ActivityType.Running,
        "snowshoeing": ActivityType.Walking,
        "wheelchair": ActivityType.Wheelchair,
        "climbing": ActivityType.Climbing,
        "roller skiing": ActivityType.RollerSkiing,
        "treadmill walking": ActivityType.Walking
    }

    _reverseActivityMappings = {
        "running": ActivityType.Running,
        "cycling sport": ActivityType.Cycling,
        "mountain biking": ActivityType.MountainBiking,
        "skating": ActivityType.Skating,
        "skiing cross country": ActivityType.CrossCountrySkiing,
        "skiing downhill": ActivityType.DownhillSkiing,
        "snowboarding": ActivityType.Snowboarding,
        "rowing": ActivityType.Rowing,
        "walking": ActivityType.Walking,
        "hiking": ActivityType.Hiking,
        "swimming": ActivityType.Swimming,
        "other": ActivityType.Other,
        "wheelchair": ActivityType.Wheelchair,
        "climbing" : ActivityType.Climbing,
        "roller skiing": ActivityType.RollerSkiing
    }

    _activitiesThatDontRoundTrip = {
        ActivityType.Cycling,
        ActivityType.Running,
        ActivityType.Walking
    }

    SupportedActivities = list(_activityMappings.values())

    def _parseDate(self, date):
        return datetime.strptime(date, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=pytz.utc)

    def _formatDate(self, date):
        return datetime.strftime(date.astimezone(pytz.utc), "%Y-%m-%d %H:%M:%S UTC")

    def _getSport(self, activity):
        # This is an activity type that doesn't round trip
        if (activity.Type in self._activitiesThatDontRoundTrip and
        # We have the original sport
        "Sport" in activity.ServiceData and
        # We know what this sport is
        activity.ServiceData["Sport"] in self._activityMappings and
        # The type didn't change (if we changed from Walking to Cycling, we'd want to let the new value through)
        activity.Type == self._activityMappings[activity.ServiceData["Sport"]]):
            return activity.ServiceData["Sport"]
        else:
            return [k for k,v in self._reverseActivityMappings.items() if v == activity.Type][0]

    def DownloadActivityList(self, serviceRecord, exhaustive=False):
      return [], []

    def UploadActivity(self, serviceRecord, activity, activitySource):
        logger.info("Activity uploading to RC ")
        activity_id = "tap-" + activity.UID + "-" + str(os.getpid())
        sport = self._getSport(activity)
        token = serviceRecord.ExternalID

        upload_data = {
            "activity_id": activity_id,
            "activity_type": sport,
            "source": activitySource.DisplayName,
            "start_time": self._formatDate(activity.StartTime),
            "end_time": self._formatDate(activity.EndTime),
            "points": [],
            "source_activity_id": activity.ServiceData['ActivityID']
        }

        if activity.Name:
            upload_data["activity_name"] = activity.Name

        if activity.Notes:
            upload_data["comments"] = activity.Notes

        if activity.Stats.Distance.Value is not None:
            upload_data["distance_in_meter"] = activity.Stats.Distance.asUnits(ActivityStatisticUnit.Meters).Value

        if activity.Stats.TimerTime.Value is not None:
            upload_data["duration_in_seconds"] = activity.Stats.TimerTime.asUnits(ActivityStatisticUnit.Seconds).Value
        elif activity.Stats.MovingTime.Value is not None:
            upload_data["duration_in_seconds"] = activity.Stats.MovingTime.asUnits(ActivityStatisticUnit.Seconds).Value
        else:
            upload_data["duration_in_seconds"] = (activity.EndTime - activity.StartTime).total_seconds()

        if activity.Stats.Energy.Value is not None:
            upload_data["calories"] = activity.Stats.Energy.asUnits(ActivityStatisticUnit.Kilocalories).Value

        elev_stats = activity.Stats.Elevation.asUnits(ActivityStatisticUnit.Meters)
        if elev_stats.Max is not None:
            upload_data["altitude_max"] = elev_stats.Max
        if elev_stats.Min is not None:
            upload_data["altitude_min"] = elev_stats.Min
        if elev_stats.Gain is not None:
            upload_data["total_ascent"] = elev_stats.Gain
        if elev_stats.Loss is not None:
            upload_data["total_descent"] = elev_stats.Loss

        speed_stats = activity.Stats.Speed.asUnits(ActivityStatisticUnit.KilometersPerHour)
        if speed_stats.Max is not None:
            upload_data["speed_max"] = speed_stats.Max

        hr_stats = activity.Stats.HR.asUnits(ActivityStatisticUnit.BeatsPerMinute)
        if hr_stats.Average is not None:
            upload_data["hr_avg"] = hr_stats.Average
        if hr_stats.Max is not None:
            upload_data["hr_max"] = hr_stats.Max

        if activity.Stats.Cadence.Average is not None:
            upload_data["cadence_avg"] = activity.Stats.Cadence.asUnits(ActivityStatisticUnit.RevolutionsPerMinute).Average
        elif activity.Stats.RunCadence.Average is not None:
            upload_data["cadence_avg"] = activity.Stats.RunCadence.asUnits(ActivityStatisticUnit.StepsPerMinute).Average

        if activity.Stats.Cadence.Max is not None:
            upload_data["cadence_max"] = activity.Stats.Cadence.asUnits(ActivityStatisticUnit.RevolutionsPerMinute).Max
        elif activity.Stats.RunCadence.Max is not None:
            upload_data["cadence_max"] = activity.Stats.RunCadence.asUnits(ActivityStatisticUnit.StepsPerMinute).Max

        for wp in activity.GetFlatWaypoints():
            pt = {
                "time": self._formatDate(wp.Timestamp),
            }
            if wp.Location:
                if wp.Location.Latitude is not None and wp.Location.Longitude is not None:
                    pt["lat"] = wp.Location.Latitude
                    pt["lng"] = wp.Location.Longitude
                if wp.Location.Altitude is not None:
                    pt["alt"] = wp.Location.Altitude
            if wp.HR is not None:
                pt["hr"] = round(wp.HR)
            if wp.Cadence is not None:
                pt["cad"] = round(wp.Cadence)
            elif wp.RunCadence is not None:
                pt["cad"] = round(wp.RunCadence)

            if wp.Type == WaypointType.Pause:
                pt["inst"] = "pause"
            elif wp.Type == WaypointType.Resume:
                pt["inst"] = "resume"
            upload_data["points"].append(pt)

        if len(upload_data["points"]):
            upload_data["points"][0]["inst"] = "start"
            upload_data["points"][-1]["inst"] = "stop"

        response = requests.post(self.UPLOAD_ACTIVITY_URL, data={"token": token, "activity_id": activity_id, "data": json.dumps(upload_data)})

        if response.status_code != 200:
            raise APIException("Could not upload activity %s %s" % (response.status_code, upload_data.text))

        return response.json()["id"]

    def DeleteCachedData(self, serviceRecord):
        pass

    def DeleteActivity(self, serviceRecord, uploadId):
        pass