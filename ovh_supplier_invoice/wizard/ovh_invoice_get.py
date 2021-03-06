# -*- encoding: utf-8 -*-
##############################################################################
#
#    OVH Supplier Invoice module for Odoo
#    Copyright (C) 2015 Akretion (http://www.akretion.com)
#    @author Alexis de Lattre <alexis.delattre@akretion.com>
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################

from openerp import models, fields, api, workflow, _
from openerp.tools import float_compare
from openerp.exceptions import Warning
from SOAPpy import WSDL
import requests
import logging
import base64
from datetime import datetime
from dateutil.relativedelta import relativedelta


logger = logging.getLogger(__name__)


class OvhInvoiceGet(models.TransientModel):
    _name = 'ovh.invoice.get'
    _description = 'Get OVH supplier invoices'

    def _default_from_date(self):
        today = datetime.today()
        return today + relativedelta(months=-1, day=1)

    auto_validate = fields.Boolean(string='Auto Validate')
    from_date = fields.Date(string='From Date', default=_default_from_date)
    attach_pdf = fields.Boolean(
        string='Attach PDF of OVH Invoice', default=True)
    account_ids = fields.One2many(
        'ovh.invoice.get.account', 'wizard_id', string='OVH Accounts')

    @api.model
    def default_get(self, fields):
        res = super(OvhInvoiceGet, self).default_get(fields)
        accounts = []
        ovh_accounts = self.env['ovh.account'].search(
            [('company_id', '=', self.env.user.company_id.id)])
        for account in ovh_accounts:
            accounts.append({
                'ovh_account_id': account.id,
                'password': account.password,
            })
        res.update(account_ids=accounts)
        return res

    @api.model
    def _prepare_invoice_line_vals(
            self, line, invoice_desc, ovh_partner,
            products, tax_id, taxrate):
        logger.debug('OVH invoice line=%s', line)
        il_fake = self.env['account.invoice.line'].browse([])
        method = invoice_desc['account'].invoice_line_method
        company = self.env.user.company_id
        if not line.baseprice:
            return False
        if method == 'no_product':
            il_vals = {
                'account_id': invoice_desc['account'].account_id.id,
                'account_analytic_id':
                invoice_desc['account'].account_analytic_id.id or False,
                'invoice_line_tax_id': [(6, 0, [tax_id])],
                }
        elif method == 'product':
            assert line.service, 'Missing service on OVH invoice line'
            product = False
            for pentry in products:
                if line.service.startswith(pentry['service']):
                    product = pentry['product']
                    break
            if not product:
                logger.debug('OVH products=%s', products)
                raise Warning(_(
                    "For OVH invoice '%s' dated %s related to account '%s', "
                    "there are no OVH product matching service '%s' "
                    "(product code: '%s').") % (
                    invoice_desc['number'],
                    invoice_desc['date'],
                    invoice_desc['account'].login,
                    line.description,
                    line.service))
            il_vals = il_fake.product_id_change(
                product.id, product.uom_id.id, type='in_invoice',
                partner_id=ovh_partner.id,
                fposition_id=ovh_partner.property_account_position.id,
                currency_id=company.currency_id.id,
                company_id=company.id)['value']
            if il_vals['invoice_line_tax_id']:
                tax = self.env['account.tax'].browse(
                    il_vals['invoice_line_tax_id'][0])
                if not tax.amount:
                    tax_amount = 0
                else:
                    tax_amount = tax.amount
                if tax_amount != taxrate:
                    raise Warning(_(
                        "For OVH invoice '%s' dated %s "
                        "related to account '%s', "
                        "the OVH product with "
                        "internal code %s "
                        "has a purchase tax '%s' (%s) with a rate %s "
                        "which is different from the rate "
                        "given by the OVH webservice (%s).") % (
                        invoice_desc['number'],
                        invoice_desc['date'],
                        invoice_desc['account'].login,
                        product.default_code,
                        tax.name or 'None',
                        tax.description or 'None',
                        tax_amount,
                        taxrate))
            il_vals.update({
                'invoice_line_tax_id':
                [(6, 0, il_vals['invoice_line_tax_id'])],
                'product_id': product.id,
                })
        il_vals.update({
            'quantity': float(line.quantity),
            'price_unit': float(line.baseprice),
            'name': line.description,
            })
        if line.start and line.end:
            start_date_str = line.start[:10]
            end_date_str = line.end[:10]
            end_date_dt = fields.Date.from_string(end_date_str)
            end_date_dt -= relativedelta(days=1)
            end_date_str = fields.Date.to_string(end_date_dt)
            il_vals['name'] = _('%s du %s au %s') % (
                line.description, start_date_str, end_date_str)
            if (
                    hasattr(il_fake, 'start_date') and
                    hasattr(il_fake, 'end_date')):
                il_vals['start_date'] = start_date_str
                il_vals['end_date'] = end_date_str
        if not il_vals.get('account_id'):
            raise Warning(_(
                "On OVH invoice '%s' dated %s related to account '%s', "
                "the account is missing on the line with description '%s'.")
                % (invoice_desc['number'],
                    invoice_desc['date'],
                    invoice_desc['account'].login,
                    il_vals.get('name')))
        return il_vals

    @api.model
    def _prepare_invoice_vals(
            self, invoice_desc, ovh_partner, res_iinfo, products):
        aio = self.env['account.invoice']
        company = self.env.user.company_id
        vals = {
            'partner_id': ovh_partner.id,
            'type': 'in_invoice',
            'company_id': company.id,
            'supplier_invoice_number': invoice_desc['number'],
            'origin': 'OVH SoAPI %s' % invoice_desc['account'].login,
            'date_invoice': res_iinfo.date[:10],
            'journal_id':
            aio.with_context(type='in_invoice')._default_journal().id,
            'invoice_line': [],
            'check_total': float(res_iinfo.finalprice),
            }
        vals.update(aio.onchange_partner_id(
            'in_invoice', ovh_partner.id, company_id=company.id)['value'])
        taxrate = float(res_iinfo.taxrate)  # =0.2 for 20%
        method = invoice_desc['account'].invoice_line_method
        tax_id = False
        if method == 'no_product':
            taxes = self.env['account.tax'].search([
                ('type_tax_use', '=', 'purchase'),
                ('amount', '=', taxrate),
                ('type', '=', 'percent'),
                ('price_include', '=', False),
                ])
            if len(taxes) < 1:
                raise Warning(_(
                    "For invoice '%s' dated %s related to account '%s', "
                    "could not find proper purchase tax in Odoo "
                    "with a rate of %s %%") % (
                    invoice_desc['number'], invoice_desc['date'],
                    invoice_desc['account'].login, taxrate * 100))
            # TODO: we take the first one, which correspond to the
            # regular tax (the other ones are IMMO-20.0 & ACH_UE_ded.-20.0)
            tax_id = taxes[0].id
        if isinstance(res_iinfo.details.item, list):
            for line in res_iinfo.details.item:
                il_vals = self._prepare_invoice_line_vals(
                    line, invoice_desc, ovh_partner,
                    products, tax_id, taxrate)
                if il_vals:
                    vals['invoice_line'].append((0, 0, il_vals))
        # When we have only 1 invoice line
        else:
            il_vals = self._prepare_invoice_line_vals(
                res_iinfo.details.item, invoice_desc,
                ovh_partner, products, tax_id, taxrate)
            if il_vals:
                vals['invoice_line'].append((0, 0, il_vals))
        return vals

    def ovh_invoice_attach_pdf(
            self, invoice, invoice_desc, invoice_password):
        logger.info(
            'Starting to download PDF of OVH invoice %s dated %s',
            invoice_desc['number'], invoice_desc['date'])
        url = 'https://www.ovh.com/cgi-bin/order/facture.pdf?'
        url += 'reference=%s&passwd=%s' % (
            invoice_desc['number'], invoice_password)
        logger.debug('OVH invoice download url: %s', url)
        rpdf = requests.get(url)
        logger.info(
            'OVH invoice PDF download HTTP code: %s', rpdf.status_code)
        if rpdf.status_code == 200:
            self.env['ir.attachment'].create({
                'name': 'OVH_invoice_%s.pdf' % invoice_desc['number'],
                'res_id': invoice.id,
                'res_model': 'account.invoice',
                'datas': base64.encodestring(rpdf.content),
                })
            logger.info(
                'Attachement created on OVH invoice %s dated %s',
                invoice_desc['number'], invoice_desc['date'])
            invoice.message_post(_(
                '<p>The PDF file of the OVH invoice has been '
                'successfully downloaded. You can get it in '
                'the attachments.'))
        else:
            logger.warning(
                'Could not download the PDF of the OVH invoice %s. '
                'HTTP error %d', invoice_desc['number'], rpdf.status_code)
            invoice.message_post(
                _('Failed to download the PDF file of the OVH '
                    'invoice (HTTP error %d') % rpdf.status_code)

    def get_ovh_products(self):
        products = self.env['product.product'].search([
            ('default_code', 'like', 'OVH-%')])
        unsorted_products = []
        sorted_products = []
        # [{'service': 'voip.sms.', 'product': product_recordset}]
        for product in products:
            unsorted_products.append(
                {'service': product.default_code[4:], 'product': product})
        # sort by len of service prefix, so that the longest prefix
        # match in priority
        if unsorted_products:
            sorted_products = sorted(
                unsorted_products,
                key=lambda mydict: len(mydict['service']) * -1)
        return sorted_products

    @api.multi
    def get(self):
        self.ensure_one()
        aio = self.env['account.invoice']
        soap = WSDL.Proxy('https://www.ovh.com/soapi/soapi-re-1.63.wsdl')
        user = self.env.user
        if not user.company_id.country_id:
            raise Warning(
                _('Missing country on company %s') % user.company_id.name)
        country_code = user.company_id.country_id.code.lower()
        partner = self.env['res.partner'].search([
            ('sanitized_vat', '=', 'FR22424761419'),
            ('parent_id', '=', False),
            ('supplier', '=', 'True')])
        if not partner:
            raise Warning(
                _("Couldn't find the supplier OVH. Make sure you have "
                    "a supplier OVH with VAT number FR22424761419."))
        ovh_partner = partner[0]
        products = self.get_ovh_products()

        invoices = aio.browse(False)
        for account in self.account_ids:
            ovh_account = account.ovh_account_id
            logger.info(
                'Opening SOAP session to OVH (account %s, country %s)',
                ovh_account.login, country_code)
            try:
                session = soap.login(
                    ovh_account.login,
                    account.password,
                    country_code, 0)
            except Exception, e:
                raise Warning(_(
                    "Cannot connect to the OVH SoAPI with login '%s'. "
                    "The error message is '%s'.")
                    % (ovh_account.login, unicode(e)))
            logger.info(
                'Starting OVH soAPI query billingInvoiceList (account %s)',
                ovh_account.login)
            res_ilist = soap.billingInvoiceList(session)
            logger.debug('result billingInvoiceList=%s', res_ilist)

            for oinv in res_ilist.item:
                oinv_num = oinv.billnum
                oinv_date = oinv.date[:10]
                invoice_desc = {
                    'number': oinv_num,
                    'date': oinv_date,
                    'account': ovh_account,  # object
                    }
                if self.from_date:
                    if oinv_date < self.from_date:
                        logger.info(
                            "Skipping OVH invoice %s dated %s related to "
                            " account %s because too old",
                            invoice_desc['number'], invoice_desc['date'],
                            invoice_desc['account'].login)
                        continue
                logger.info(
                    "billingInvoiceList: OVH invoice number %s dated %s "
                    "related to account %s",
                    invoice_desc['number'], invoice_desc['date'],
                    invoice_desc['account'].login)
                if not oinv.totalPrice and not oinv.totalPriceWithVat:
                    logger.info(
                        'Skipping OVH invoice %s dated %s related to '
                        'account %s because the amount is 0',
                        invoice_desc['number'], invoice_desc['date'],
                        invoice_desc['account'].login)
                    continue
                if oinv_num and oinv_num.startswith('PP_'):
                    logger.info(
                        'Skipping OVH invoice %s dated %s related to '
                        'account %s because it is a '
                        'special pre-paid invoice',
                        invoice_desc['number'], invoice_desc['date'],
                        invoice_desc['account'].login)
                    continue
                # Check if this invoice is not already in the system
                existing_inv = aio.search([
                    ('type', '=', 'in_invoice'),
                    ('partner_id', '=', ovh_partner.id),
                    ('supplier_invoice_number', '=', oinv_num),
                    ])
                if existing_inv:
                    logger.warning(
                        'The OVH invoice number %s dated %s already '
                        'exists in Odoo',
                        invoice_desc['number'], invoice_desc['date'])
                    continue
                logger.info(
                    'Starting OVH soAPI query billingInvoiceInfo on OVH '
                    'invoice number %s dated %s',
                    invoice_desc['number'], invoice_desc['date'])
                res_iinfo = soap.billingInvoiceInfo(
                    session, oinv_num, account.password, country_code)
                logger.debug(
                    'Result billingInvoiceInfo for invoice %s: %s',
                    oinv_num, res_iinfo)
                vals = self._prepare_invoice_vals(
                    invoice_desc, ovh_partner, res_iinfo, products)
                invoice = aio.create(vals)
                invoice.button_reset_taxes()
                logger.debug(
                    'res_iinfo.finalprice=%s ; invoice.amount_total=%s',
                    res_iinfo.finalprice, invoice.amount_total)
                prec = self.env['decimal.precision'].precision_get('Account')
                if float_compare(
                        float(res_iinfo.baseprice),
                        invoice.amount_untaxed,
                        precision_digits=prec):
                    raise Warning(_(
                        "For OVH invoice '%s' dated %s related to "
                        "account '%s', "
                        "the total untaxed amount is %.2f "
                        "whereas the total untaxed amount in Odoo is %.2f.")
                        % (invoice_desc['number'], invoice_desc['date'],
                            invoice_desc['account'].login, res_iinfo.baseprice,
                            invoice.amount_untaxed))

                if float_compare(
                        float(res_iinfo.finalprice),
                        invoice.amount_total,
                        precision_digits=prec):
                    # we should force the VAT amount
                    assert invoice.tax_line, 'Invoice has no tax line'
                    native_vat_amount = invoice.tax_line[0].amount
                    invoice.tax_line[0].amount = float(res_iinfo.tax)
                    invoice.message_post(
                        'The total tax amount has been forced to %.2f %s '
                        '(initial amount: %.2f).'
                        % (float(res_iinfo.tax), invoice.currency_id.symbol,
                            native_vat_amount))
                invoices += invoice
                invoice.message_post(_(
                    '<p>This OVH invoice has been downloaded automatically '
                    'via the SoAPI with OVH account %s.</p>'
                    '<ul>'
                    '<li>Total without taxes: %s</li>'
                    '<li>Total VAT: %s</li>'
                    '<li>Total with taxes: %s</li>'
                    '</ul>')
                    % (ovh_account.login, res_iinfo.baseprice, res_iinfo.tax,
                        res_iinfo.finalprice))
                # Attach PDF
                if self.attach_pdf:
                    self.ovh_invoice_attach_pdf(
                        invoice, invoice_desc, res_iinfo.password)
                # Validate invoice
                if self.auto_validate:
                    workflow.trg_validate(
                        self._uid, aio._name, invoice.id,
                        'invoice_open', self._cr)

        # delete the wizard entry, to avoid leaving passwords in DB
        # in table ovh_invoice_get_account
        self.unlink()
        action = self.env['ir.actions.act_window'].for_xml_id(
            'account', 'action_invoice_tree2')
        action.update({
            'view_mode': 'tree,form,calendar,graph',
            'domain': "[('id', 'in', %s)]" % invoices.ids,
            'views': False,
            'nodestroy': False,
            })
        return action


class OvhInvoiceGetAccount(models.TransientModel):
    _name = 'ovh.invoice.get.account'
    _description = 'OVH Invoice Get Account'

    wizard_id = fields.Many2one(
        'ovh.invoice.get', string='Wizard', ondelete='cascade')
    ovh_account_id = fields.Many2one(
        'ovh.account', string='OVH Account', required=True)
    password = fields.Char(string='OVH Password')
