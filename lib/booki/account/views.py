import datetime
import traceback
from django.shortcuts import render_to_response
from django.template.loader import render_to_string
from django.core.paginator import Paginator, InvalidPage, EmptyPage
from django.core.urlresolvers import reverse
from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.db import IntegrityError, transaction
from django.contrib.auth.decorators import login_required
try:
    from django.core.validators import email_re
except:
    from django.forms.fields import email_re

from django import forms

from booki.utils import pages
from django.utils.translation import ugettext as _

from booki.utils.log import logBookHistory, logError
from booki.utils.book import createBook
from booki.editor import common

from booki.messaging.views import get_endpoint_or_none

try:
    ESPRI_URL = settings.ESPRI_URL
    TWIKI_GATEWAY_URL = settings.TWIKI_GATEWAY_URL
except AttributeError:
    # for backwards compatibility
    ESPRI_URL = "http://objavi.flossmanuals.net/espri.cgi"
    TWIKI_GATEWAY_URL = "http://objavi.flossmanuals.net/booki-twiki-gateway.cgi"
    
try:
    THIS_BOOKI_SERVER = settings.THIS_BOOKI_SERVER
except AttributeError:
    import os
    THIS_BOOKI_SERVER = os.environ.get('HTTP_HOST', 'www.booki.cc')

    
def view_accounts(request):
    """
    Django View for /accounts/ url. Does nothing at the moment.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request

    @todo: This has to change as soon as possible. Redirect somewhere? Show list of current users?
    """
    return HttpResponse("", mimetype="text/plain")

# signout

def signout(request):
    """
    Django View. Gets called when user wants to signout.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    """

    from django.contrib import auth

    auth.logout(request)

    return HttpResponseRedirect(reverse("frontpage"))

# signin

@transaction.commit_manually
def signin(request):
    """
    Django View. Gets called when user wants to signin or create new account.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    """


    from booki.utils.json_wrapper import simplejson

    from booki.editor.models import BookiGroup

    from django.core.exceptions import ObjectDoesNotExist
    from django.contrib import auth

    if request.POST.get("ajax", "") == "1":
        ret = {"result": 0}

        if request.POST.get("method", "") == "register":
            def _checkIfEmpty(key):
                return request.POST.get(key, "").strip() == ""

            def _doChecksForEmpty():
                if _checkIfEmpty("username"): return 2
                if _checkIfEmpty("email"): return 3
                if _checkIfEmpty("password") or _checkIfEmpty("password2"): return 4
                if _checkIfEmpty("fullname"): return 5

                return 0

            ret["result"] = _doChecksForEmpty()

            if ret["result"] == 0: # if there was no errors
                import re

                def _doCheckValid():
                    # check if it is valid username
                    # - from 2 to 20 characters long
                    # - word, number, ., _, -
                    mtch = re.match('^[\w\d\_\.\-]{2,20}$', request.POST.get("username", "").strip())
                    if not mtch:  return 6

                    # check if it is valid email
                    if not bool(email_re.match(request.POST["email"].strip())): return 7

                    if request.POST.get("password", "") != request.POST.get("password2", "").strip(): return 8
                    if len(request.POST.get("password", "").strip()) < 6: return 9

                    # check if this user exists
                    try:
                        u = auth.models.User.objects.get(username=request.POST.get("username", ""))
                        return 10
                    except auth.models.User.DoesNotExist:
                        pass

                    return 0

                ret["result"] = _doCheckValid()

                if ret["result"] == 0:
                    ret["result"] = 1

                    try:
                        user = auth.models.User.objects.create_user(username=request.POST["username"],
                                                                    email=request.POST["email"],
                                                                    password=request.POST["password"])
                    except IntegrityError:
                        ret["result"] = 10

                    # this is not a good place to fire signal, but i need password for now
                    # should create function createUser for future use

                    import booki.account.signals
                    booki.account.signals.account_created.send(sender = user, password = request.POST["password"])

                    user.first_name = request.POST["fullname"]

                    try:
                        user.save()

                        # groups

                        for groupName in simplejson.loads(request.POST.get("groups")):
                            if groupName.strip() != '':
                                sid = transaction.savepoint()

                                try:
                                    group = BookiGroup.objects.get(url_name=groupName)
                                    group.members.add(user)
                                except:
                                    transaction.savepoint_rollback(sid)
                                else:
                                    transaction.savepoint_commit(sid)

                        user2 = auth.authenticate(username=request.POST["username"], password=request.POST["password"])
                        auth.login(request, user2)
                    except:
                        transaction.rollback()
                    else:
                        transaction.commit()


        if request.POST.get("method", "") == "signin":
            user = auth.authenticate(username=request.POST["username"], password=request.POST["password"])

            if user:
                auth.login(request, user)
                ret["result"] = 1
            else:
                try:
                    usr = auth.models.User.objects.get(username=request.POST["username"])
                    # User does exist. Must be wrong password then
                    ret["result"] = 3
                except auth.models.User.DoesNotExist:
                    # User does not exist
                    ret["result"] = 2

        transaction.commit()
        return HttpResponse(simplejson.dumps(ret), mimetype="text/json")

    redirect = request.GET.get('redirect', reverse('frontpage'))

    if request.GET.get('next', None):
        redirect = request.GET.get('next')


    joinGroups = []
    for groupName in request.GET.getlist("group"):
        try:
            joinGroups.append(BookiGroup.objects.get(url_name=groupName))
        except BookiGroup.DoesNotExist:
            pass

    try:
        return render_to_response('account/signin.html', {"request": request, 'redirect': redirect, 'joingroups': joinGroups})
    except:
        transaction.rollback()
    finally:
        transaction.commit()


# forgotpassword
@transaction.commit_manually
def forgotpassword(request):
    """
    Django View. Gets called when user wants to change password he managed to forget.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    """

    from booki.utils.json_wrapper import simplejson
    from django.core.exceptions import ObjectDoesNotExist
    from django.contrib.auth.models import User

    if request.POST.get("ajax", "") == "1":
        ret = {"result": 0}
        usr = None

        if request.POST.get("method", "") == "forgot_password":
            def _checkIfEmpty(key):
                return request.POST.get(key, "").strip() == ""

            def _doChecksForEmpty():
                if _checkIfEmpty("username"): return 2
                return 0

            ret["result"] = _doChecksForEmpty()

            if ret["result"] == 0:
                allOK = True
                try:
                    usr = User.objects.get(username=request.POST.get("username", ""))
                except User.DoesNotExist:
                    pass

                if not usr:
                    try:
                        usr = User.objects.get(email=request.POST.get("username", ""))
                    except User.DoesNotExist:
                        allOK = False

                if allOK:
                    from booki.account import models as account_models
                    from django.core.mail import send_mail

                    def generateSecretCode():
                        import string
                        from random import choice
                        return ''.join([choice(string.letters + string.digits) for i in range(30)])

                    secretcode = generateSecretCode()

                    account_models = account_models.UserPassword()
                    account_models.user = usr
                    account_models.remote_useragent = request.META.get('HTTP_USER_AGENT','')
                    account_models.remote_addr = request.META.get('REMOTE_ADDR','')
                    account_models.remote_host = request.META.get('REMOTE_HOST','')
                    account_models.secretcode = secretcode

                    try:
                        account_models.save()
                    except:
                        transaction.rollback()
                    else:
                        transaction.commit()

                    #
                    body = render_to_string('account/password_reset_email.txt', 
                                            dict(secretcode=secretcode))
                    send_mail(_('Reset password'), body,
                              'info@' + THIS_BOOKI_SERVER,
                              [usr.email], fail_silently=False)

                else:
                    ret["result"] = 3


        return HttpResponse(simplejson.dumps(ret), mimetype="text/json")

    try:
        return render_to_response('account/forgot_password.html', {"request": request})
    except:
        transaction.rollback()
    finally:
        transaction.commit()


# forgotpasswordenter
@transaction.commit_manually
def forgotpasswordenter(request):
    """
    Django View. Gets called when user clicks on the link he recieved over email.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    """

    from booki.utils.json_wrapper import simplejson

    secretcode = request.GET.get('secretcode', '')

    from django.core.exceptions import ObjectDoesNotExist
    from django.contrib.auth.models import User

    if request.POST.get("ajax", "") == "1":
        ret = {"result": 0}
        usr = None

        if request.POST.get("method", "") == "forgot_password_enter":
            def _checkIfEmpty(key):
                return request.POST.get(key, "").strip() == ""

            def _doChecksForEmpty():
                if _checkIfEmpty("secretcode"): return 2
                if _checkIfEmpty("password1"): return 3
                if _checkIfEmpty("password2"): return 4

                return 0

            ret["result"] = _doChecksForEmpty()

            if ret["result"] == 0:

                from booki.account import models as account_models
                allOK = True

                try:
                    pswd = account_models.UserPassword.objects.get(secretcode=request.POST.get("secretcode", ""))
                except account_models.UserPassword.DoesNotExist:
                    allOK = False

                if allOK:
                    pswd.user.set_password(request.POST.get("password1", ""))
                    pswd.user.save()
                else:
                    ret["result"] = 5


        transaction.commit()

        return HttpResponse(simplejson.dumps(ret), mimetype="text/json")

    try:
        return render_to_response('account/forgot_password_enter.html', {"request": request, "secretcode": secretcode})
    except:
        transaction.rollback()
    finally:
        transaction.commit()

# project form

class BookForm(forms.Form):
    """
    Django Form for new books.

    @todo: This is major c* and has to be changed soon.
    """

    title = forms.CharField(label=_('Title'), required=True)
    license = forms.ChoiceField(label=_('License'), choices=(('1', '1'), ))
    hidden = forms.BooleanField(label=_('Initially hidden from others'), required=False)

    def __init__(self, *args, **kwargs):
        super(BookForm, self).__init__(*args, **kwargs)

        from booki.editor import models
        self.fields['license'].initial = 'Unknown'
        self.fields['license'].choices = [ (elem.abbrevation, elem.name) for elem in models.License.objects.all().order_by("name")]


class ImportForm(forms.Form):
    """
    Django Form for book imports.

    @todo: This is major c* and has to be changed soon.
    """

    type = forms.CharField(required=True)
    id = forms.CharField(required=True)
    rename_title = forms.CharField(required=False)
    hidden = forms.BooleanField(label=_('Initially hidden from others'), required=False)

class ImportEpubForm(forms.Form):
    url = forms.CharField(required=False)

class ImportWikibooksForm(forms.Form):
    wikibooks_id = forms.CharField(required=False)

class ImportFlossmanualsForm(forms.Form):
    flossmanuals_id = forms.CharField(required=False)
    type = forms.CharField(required=False)
    id = forms.CharField(required=False)



def view_profile(request, username):
    """
    Django View. Shows user profile. Right now, this is just basics.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    @type username: C{string}
    @param username: Username.

    @todo: Check if user exists. 
    """

    from django.contrib.auth.models import User
    from booki.editor import models

    from django.template.defaultfilters import slugify

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return pages.ErrorPage(request, "errors/user_does_not_exist.html", {"username": username})

    books = models.Book.objects.filter(owner=user, hidden=False)
    
    groups = user.members.all()
    return render_to_response('account/view_profile.html', {"request": request,
                                                            "user": user,

                                                            "books": books,
                                                            "groups": groups})


## user settings

class SettingsForm(forms.Form):
    """
    Django Form for Settings. This should change in the future (and not use Django Forms at all).
    """

    email = forms.EmailField(required=True, label=_('Email'))
    firstname = forms.CharField(required=True, label=_('Full name'))
#    lastname = forms.CharField(required=True, label='Last name')
    description = forms.CharField(widget=forms.Textarea(), required=False, label=_("Blurb about yourself"))

    image = forms.Field(widget=forms.FileInput(), required=False, label=_('Image'))
    notification_filter = forms.CharField(required=False, label=_('Notification filter'))

## user settings

@transaction.commit_manually
def user_settings(request, username):
    """
    Django View. Edit user settings. Right now, this is just basics.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    @type username: C{string}
    @param username: Username.

    @todo: Check if user exists. 
    """

    from django.contrib.auth.models import User
    from booki.editor import models

    from django.template.defaultfilters import slugify

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return pages.ErrorPage(request, "errors/user_does_not_exist.html", {"username": username})


    if request.method == 'POST':
        settings_form = SettingsForm(request.POST, request.FILES)
        if settings_form.is_valid():

            # this is very stupid and wrong
            # change the way it works
            # make utils for
            #     - get image url
            #     - get image path
            #     - seperate storage for

            from django.core.files import File
            profile = user.get_profile()

            user.email      = settings_form.cleaned_data['email']
            user.first_name = settings_form.cleaned_data['firstname']
            #user.last_name  = settings_form.cleaned_data['lastname']
            user.save()

            profile.description = settings_form.cleaned_data['description']

            # this works for now, but this kind of processing must be done somewhere else!

            if settings_form.cleaned_data['image']:
                import tempfile
                import os

                # check this later
                fh, fname = tempfile.mkstemp(suffix='', prefix='profile')

                f = open(fname, 'wb')
                for chunk in settings_form.cleaned_data['image'].chunks():
                    f.write(chunk)
                f.close()

                import Image

                im = Image.open(fname)
                im.thumbnail((120, 120), Image.NEAREST)
                imageName = '%s.jpg' % fname
                im.save(imageName)

                profile.image.save('%s.jpg' % user.username, File(file(imageName)))
                os.unlink(fname)

            profile.save()

            endpoint_config = get_endpoint_or_none("@"+user.username).get_config()
            endpoint_config.notification_filter = settings_form.cleaned_data['notification_filter']
            endpoint_config.save()

            transaction.commit()

            return HttpResponseRedirect(reverse("view_profile", args=[username]))
    else:
        settings_form = SettingsForm(initial = {'image': 'aaa',
                                                'firstname': user.first_name,
                                                #'lastname': user.last_name,
                                                'description': user.get_profile().description,
                                                'email': user.email,
                                                'notification_filter': get_endpoint_or_none("@"+user.username).notification_filter()})

    try:
        return render_to_response('account/user_settings.html', {"request": request,
                                                                 "user": user,
                                                                 
                                                                 "settings_form": settings_form,
                                                                 })
    except:
        transaction.rollback()
    finally:
        transaction.commit()


def view_profilethumbnail(request, profileid):
    """
    Django View. Shows user profile image.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    @type profileid: C{string}
    @param profileid: Username.

    @todo: Check if user exists. 
    """

    from django.http import HttpResponse

    from django.contrib.auth.models import User

    try:
        u = User.objects.get(username=profileid)
    except User.DoesNotExist:
        return pages.ErrorPage(request, "errors/user_does_not_exist.html", {"username": username})


    # this should be a seperate function

    if not u.get_profile().image:
        name = '%s%s' % (settings.SITE_STATIC_ROOT, '/images/anonymous.jpg')
    else:
        name =  u.get_profile().image.name

    import Image

    image = Image.open(name)
    image.thumbnail((int(request.GET.get('width', 24)), int(request.GET.get('width', 24))), Image.NEAREST)

    # serialize to HTTP response
    response = HttpResponse(mimetype="image/jpg")
    image.save(response, "JPEG")
    return response


@transaction.commit_manually
def my_books (request, username):
    """
    Django View. Show user books.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    @type username: C{string}
    @param username: Username.
    """

    from django.contrib.auth.models import User
    from booki.editor import models
    from django.template.defaultfilters import slugify

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return pages.ErrorPage(request, "errors/user_does_not_exist.html", {"username": username})
        
    books = models.Book.objects.filter(owner=user)

    if request.POST.get("action") == "hide":
        book = models.Book.objects.get(url_title=request.POST.get("book"))
        book.hidden = True
        book.save()
        transaction.commit()
    elif request.POST.get("action") == "unhide":
        book = models.Book.objects.get(url_title=request.POST.get("book"))
        book.hidden = False
        book.save()
        transaction.commit()

    if request.method == 'POST' and not request.POST.get("action"):
        project_form = BookForm(request.POST)
        import_form = ImportForm(request.POST)

        if import_form.is_valid() and import_form.cleaned_data["id"]:
            project_form = BookForm() # reset the other form

            try:
                ID = import_form.cleaned_data["id"]
                import_type = import_form.cleaned_data["type"]
                rename_title = import_form.cleaned_data["rename_title"]

                extraOptions = {}
                if rename_title:
                    extraOptions['book_title'] = rename_title

                if import_form.cleaned_data["hidden"]:
                    extraOptions['hidden'] = True

                import_sources = {   # base_url    source=
                    'flossmanuals': (TWIKI_GATEWAY_URL, "en.flossmanuals.net"),
                    "archive":      (ESPRI_URL, "archive.org"),
                    "wikibooks":    (ESPRI_URL, "wikibooks"),
                    "epub":         (ESPRI_URL, "url"),
                    }

                if import_type == "booki":
                    ID = ID.rstrip("/")
                    booki_url, book_url_title = ID.rsplit("/", 1)
                    base_url = "%s/export/%s/export" % (booki_url, book_url_title)
                    source = "booki"
                else:
                    base_url, source = import_sources[import_type]

                common.importBookFromUrl2(user, base_url,
                                          args=dict(source=source,
                                                    book=ID),
                                          **extraOptions
                                          )
            except Exception:
                transaction.rollback()
                logError(traceback.format_exc())
                return render_to_response('account/error_import.html',
                                          {"request": request, "user": user})
            else:
                transaction.commit()

        #XXX should this be elif? even if the POST is valid as both forms, the
        # transaction will end up being commited twice.
        if project_form.is_valid() and project_form.cleaned_data["title"]:
            import_form = ImportForm() # reset the other form
            
            from booki.utils.book import createBook
            title = project_form.cleaned_data["title"]

            try:
                book = createBook(user, title)

                license   = project_form.cleaned_data["license"]
                lic = models.License.objects.get(abbrevation=license)
                book.license = lic
                book.hidden = project_form.cleaned_data["hidden"]
                book.save()
            except:
                transaction.rollback()
            else:
                transaction.commit()

            return HttpResponseRedirect(reverse("my_books", args=[username]))
    else:
        project_form = BookForm()
        import_form = ImportForm()


    try:
        return render_to_response('account/my_books.html', {"request": request,
                                                            "user": user,
                                                            
                                                            "project_form": project_form,
                                                            "import_form": import_form,
                                                            
                                                            "books": books,})
    except:
        transaction.rollback()
    finally:
        transaction.commit()

def my_groups (request, username):
    """
    Django View. Show user groups.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    @type username: C{string}
    @param username: Username.
    """

    from django.contrib.auth.models import User

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return pages.ErrorPage(request, "errors/user_does_not_exist.html", {"username": username})

    groups = user.members.all()

    return render_to_response('account/my_groups.html', {"request": request,
                                                            "user": user,

                                                            "groups": groups,})


def my_people (request, username):
    """
    Django View. Shows nothing at the moment.

    @type request: C{django.http.HttpRequest}
    @param request: Django Request
    @type username: C{string}
    @param username: Username.
    """

    from django.contrib.auth.models import User

    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        return pages.ErrorPage(request, "errors/user_does_not_exist.html", {"username": username})

    return render_to_response('account/my_people.html', {"request": request,
                                                         "user": user})

