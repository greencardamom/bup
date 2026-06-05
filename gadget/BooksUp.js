/**
 * BooksUp — surface book and journal links for the current article (from the
 * bup tool's API) and apply them in the edit window so the editor can
 * review/modify before saving.
 *
 * Install: copy to User:GreenC/BooksUp.js, then in User:GreenC/common.js add:
 *   mw.loader.load('//en.wikipedia.org/w/index.php?title=User:GreenC/BooksUp.js&action=raw&ctype=text/javascript');
 *
 * Source / issues: https://github.com/greencardamom/bup
 */
/* global mw, $ */
( function () {
	'use strict';

	// ---- config -----------------------------------------------------------
	var API = 'https://bup.toolforge.org/api/v1';   // bup read-only API
	var DOC = 'User:GreenC/BooksUp';                 // linked in the edit summary
	var STASH_KEY = 'BooksUp.pending';               // read-page -> edit-page handoff

	function editSummary( n ) {
		return 'Adding book link' + ( n === 1 ? '' : 's' ) +
			' ([[' + DOC + '|BooksUp]])';
	}

	// ---- helpers ----------------------------------------------------------

	// Build /page/<title>, keeping "/" as path separators (route is <path:title>).
	function apiPageUrl( title ) {
		return API + '/page/' + encodeURIComponent( title ).replace( /%2F/gi, '/' );
	}

	function fetchCandidates( title ) {
		return fetch( apiPageUrl( title ), { headers: { Accept: 'application/json' } } )
			.then( function ( r ) {
				if ( r.status === 404 ) { return { found: false, citations: [] }; }
				if ( !r.ok ) { throw new Error( 'API HTTP ' + r.status ); }
				return r.json();
			} );
	}

	function fetchWikitext( title ) {
		return new mw.Api().get( {
			action: 'query', prop: 'revisions', titles: title,
			rvprop: 'content', rvslots: 'main', formatversion: 2, format: 'json'
		} ).then( function ( d ) {
			try {
				return d.query.pages[ 0 ].revisions[ 0 ].slots.main.content;
			} catch ( e ) {
				return '';
			}
		} );
	}

	// Literal replace of every occurrence (matches how bup applies edits).
	function applyAll( wikitext, cites ) {
		cites.forEach( function ( c ) {
			wikitext = wikitext.split( c.oldcite ).join( c.newcite );
		} );
		return wikitext;
	}

	// ---- edit-page side: drop a stashed change into the edit form ---------

	function applyStashToEditor() {
		var raw = sessionStorage.getItem( STASH_KEY );
		if ( !raw ) { return; }
		sessionStorage.removeItem( STASH_KEY );

		var data;
		try { data = JSON.parse( raw ); } catch ( e ) { return; }
		if ( !data || data.page !== mw.config.get( 'wgPageName' ) ) { return; }

		var $text = $( '#wpTextbox1' );
		if ( !$text.length ) { return; }
		$text.val( data.text ).trigger( 'change' ).trigger( 'input' );

		var $sum = $( '#wpSummary, #wpSummaryWidget input' );
		if ( $sum.length && !$sum.val() ) { $sum.val( data.summary ); }

		mw.notify(
			'Applied ' + data.count + ' change' + ( data.count === 1 ? '' : 's' ) +
			'. Review the diff (Show changes) and save.',
			{ title: 'BooksUp', autoHide: false }
		);
	}

	// ---- read-page side: the suggestions panel ----------------------------

	function injectStyle() {
		if ( document.getElementById( 'booksup-style' ) ) { return; }
		mw.util.addCSS(
			'#booksup-panel{position:fixed;top:80px;right:16px;z-index:1000;width:420px;' +
				'max-height:80vh;overflow:auto;background:#fff;border:1px solid #a2a9b1;' +
				'border-radius:4px;box-shadow:0 2px 8px rgba(0,0,0,.2);font-size:13px;padding:0}' +
			'#booksup-panel h3{margin:0;padding:8px 12px;background:#36c;color:#fff;' +
				'font-size:14px;border-radius:4px 4px 0 0}' +
			'#booksup-panel .booksup-body{padding:8px 12px}' +
			'#booksup-panel ul{list-style:none;margin:0;padding:0}' +
			'#booksup-panel li{padding:6px 0;border-bottom:1px solid #eaecf0}' +
			'#booksup-panel pre{white-space:pre-wrap;word-break:break-word;background:#f8f9fa;' +
				'border:1px solid #eaecf0;padding:4px;margin:4px 0;font-size:11px}' +
			'#booksup-panel pre.new{background:#eaf3ff}' +
			'#booksup-panel .booksup-foot{padding:8px 12px;border-top:1px solid #eaecf0;text-align:right}' +
			'#booksup-panel button{margin-left:6px}'
		);
		$( '<span id="booksup-style">' ).appendTo( 'head' );
	}

	function closePanel() {
		$( '#booksup-panel' ).remove();
	}

	function showPanel( title, wikitext, cites ) {
		injectStyle();
		closePanel();

		var $panel = $( '<div id="booksup-panel">' );
		$( '<h3>' ).text( 'BooksUp — ' + cites.length + ' suggestion' +
			( cites.length === 1 ? '' : 's' ) ).appendTo( $panel );

		var $body = $( '<div class="booksup-body">' ).appendTo( $panel );
		var $ul = $( '<ul>' ).appendTo( $body );

		cites.forEach( function ( c, i ) {
			var $li = $( '<li>' );
			var $label = $( '<label>' );
			$( '<input type="checkbox" checked>' )
				.attr( 'data-i', i ).appendTo( $label );
			$( '<a target="_blank" rel="noopener">' )
				.attr( 'href', c.url ).text( ' ' + c.iaid + ' (' + c.type + ')' )
				.appendTo( $label );
			$label.appendTo( $li );

			var $det = $( '<details>' ).appendTo( $li );
			$( '<summary>' ).text( 'show citation' ).appendTo( $det );
			$( '<pre class="old">' ).text( c.oldcite ).appendTo( $det );
			$( '<pre class="new">' ).text( c.newcite ).appendTo( $det );
			$li.appendTo( $ul );
		} );

		var $foot = $( '<div class="booksup-foot">' ).appendTo( $panel );
		$( '<button class="mw-ui-button">' ).text( 'Close' )
			.on( 'click', closePanel ).appendTo( $foot );
		$( '<button class="mw-ui-button mw-ui-progressive">' ).text( 'Open in editor' )
			.on( 'click', function () {
				var chosen = [];
				$panel.find( 'input:checked' ).each( function () {
					chosen.push( cites[ $( this ).attr( 'data-i' ) ] );
				} );
				if ( !chosen.length ) {
					mw.notify( 'BooksUp: nothing selected.', { title: 'BooksUp' } );
					return;
				}
				var newtext = applyAll( wikitext, chosen );
				sessionStorage.setItem( STASH_KEY, JSON.stringify( {
					page: title, text: newtext,
					summary: editSummary( chosen.length ), count: chosen.length
				} ) );
				window.location.href = mw.util.getUrl( title, { action: 'edit' } );
			} ).appendTo( $foot );

		$( document.body ).append( $panel );
	}

	function run() {
		var title = mw.config.get( 'wgPageName' );
		mw.notify( 'Checking…', { title: 'BooksUp', tag: 'booksup' } );

		Promise.all( [ fetchCandidates( title ), fetchWikitext( title ) ] )
			.then( function ( res ) {
				var cites = ( res[ 0 ] && res[ 0 ].citations ) || [];
				var wikitext = res[ 1 ] || '';
				// keep only candidates whose oldcite is literally present now
				var applicable = cites.filter( function ( c ) {
					return c.oldcite && wikitext.indexOf( c.oldcite ) !== -1;
				} );
				if ( !applicable.length ) {
					mw.notify( 'No applicable suggestions for this article.',
						{ title: 'BooksUp', tag: 'booksup' } );
					return;
				}
				showPanel( title, wikitext, applicable );
			} )
			.catch( function ( err ) {
				mw.notify( 'Error: ' + err.message, { title: 'BooksUp', type: 'error', tag: 'booksup' } );
			} );
	}

	// ---- init -------------------------------------------------------------

	var action = mw.config.get( 'wgAction' );

	if ( action === 'edit' || action === 'submit' ) {
		// Coming back from the panel: fill the edit form once it's ready.
		mw.hook( 'wikipage.editform' ).add( applyStashToEditor );
		return;
	}

	if ( mw.config.get( 'wgNamespaceNumber' ) === 0 && action === 'view' ) {
		mw.loader.using( [ 'mediawiki.util', 'mediawiki.api' ] ).then( function () {
			var link = mw.util.addPortletLink(
				'p-tb', '#', 'BooksUp', 't-booksup',
				'Find book links for this article'
			);
			if ( link ) {
				$( link ).on( 'click', function ( e ) {
					e.preventDefault();
					run();
				} );
			}
		} );
	}
}() );
